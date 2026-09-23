#!/usr/bin/env python
"""Desktop interface and independently callable safe batch runner for the tuner."""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import os
from pathlib import Path
import queue
import stat
import threading
import time


def _is_link(path: Path) -> bool:
    """Include Windows junctions and other reparse points on Python 3.10+."""
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def validate_paths(input_dir, output_dir) -> tuple[Path, Path]:
    if not str(input_dir).strip() or not str(output_dir).strip():
        raise ValueError("Choose both the original sample folder and an output folder.")
    source = Path(str(input_dir).strip()).expanduser().absolute()
    destination = Path(str(output_dir).strip()).expanduser().absolute()
    for folder in (source, destination):
        if any(_is_link(part) for part in (folder, *folder.parents)):
            raise ValueError("Use a regular folder, rather than a link or junction.")
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise ValueError("The original sample folder does not exist.")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Choose a separate output folder outside the original sample folder.\n"
                         "Neither folder can contain the other.")
    if destination.exists():
        if not destination.is_dir() or next(destination.iterdir(), None) is not None:
            raise ValueError("The output folder must be empty or new, so nothing gets overwritten.")
    return source, destination


def process_pack(input_dir, output_dir, suffix="_C", normalize=True,
                 emit=None, cancel_event=None, _tuner=None) -> dict:
    """Tune a pack. emit receives event dictionaries; no GUI dependencies here.

    cancel_event is a threading.Event. Cancellation finishes the current WAV and
    leaves a valid partial CSV report. _tuner is an optional test double exposing
    tune_file(path, destination, argparse.Namespace).
    """
    emit = emit or (lambda event: None)
    cancel_event = cancel_event or threading.Event()
    started = time.monotonic()
    source, destination = validate_paths(input_dir, output_dir)
    if suffix not in ("", "_C"):
        raise ValueError("Filename suffix must be _C or empty.")
    emit({"type": "status", "message": "Finding WAV files…"})
    wavs, skipped = [], 0

    def scan_error(error):
        raise error

    for root, dirs, files in os.walk(source, followlinks=False, onerror=scan_error):
        folder = Path(root)
        regular_dirs = [name for name in dirs if not _is_link(folder / name)]
        skipped += len(dirs) - len(regular_dirs)
        dirs[:] = sorted(regular_dirs, key=str.casefold)
        for name in sorted(files, key=str.casefold):
            path = folder / name
            if path.suffix.lower() != ".wav":
                continue
            if _is_link(path) or not path.is_file():
                skipped += 1
            else:
                wavs.append(path)
        if cancel_event.is_set():
            break
    if not wavs and not cancel_event.is_set():
        raise ValueError("No WAV files were found in that folder or its subfolders.")
    emit({"type": "status", "message": "Loading the audio engine. The first file may take a moment…"})
    if _tuner is None and not cancel_event.is_set():
        import ostirus_tune_to_c as _tuner
    validate_paths(source, destination)
    destination.mkdir(parents=True, exist_ok=True)
    report_path = destination / "tune_to_c_report.csv"
    counts = {"total": len(wavs), "processed": 0, "tuned": 0, "uncertain": 0,
              "errors": 0, "skipped_links": skipped}
    args = argparse.Namespace(max_analysis_sec=1.0, min_hz=24.0,
                              min_confidence=4.0, max_shift_semitones=6.0,
                              normalize_peak_db=-1.0 if normalize else None)
    emit({"type": "started", **counts})
    with report_path.open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source_file", "output_file", "status", "source_peak_hz",
                         "target_c_hz", "semitones", "confidence", "target_note", "message"])
        handle.flush()
        for wav in wavs:
            if cancel_event.is_set():
                break
            relative = wav.relative_to(source)
            dest = destination / relative.parent / f"{wav.stem}{suffix}{wav.suffix}"
            emit({"type": "current", "file": str(relative), **counts})
            info, error = None, ""
            try:
                if _is_link(wav) or not wav.resolve().is_relative_to(source):
                    raise ValueError("Source became a link or moved outside the input folder.")
                if any(_is_link(part) for part in (wav.parent, *wav.parent.parents)):
                    raise ValueError("Source folder became a link or junction.")
                if dest.exists() or _is_link(dest):
                    raise FileExistsError("Output already exists; it was not overwritten.")
                if any(_is_link(part) for part in (dest.parent, *dest.parent.parents)):
                    raise ValueError("Output folder became a link or junction.")
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    info = _tuner.tune_file(wav, dest, args)
                if info.note == "copied_low_confidence":
                    status, bucket = "copied_uncertain", "uncertain"
                    detail = "Uncertain pitch · copied unchanged"
                elif info.note == "copied_processing_error":
                    status, bucket = "copied_error", "errors"
                    detail = "Processing error · copied unchanged"
                else:
                    status, bucket = "tuned", "tuned"
                    detail = f"{info.note} · {info.semitones:+.2f} semitones"
                error = info.message
            except Exception as exc:
                status, bucket, error = "error", "errors", str(exc)
                detail = "Could not process this file"
            counts["processed"] += 1
            counts[bucket] += 1
            writer.writerow([str(relative), str(dest.relative_to(destination)), status,
                             *([f"{info.source_hz:.3f}", f"{info.target_hz:.3f}",
                                f"{info.semitones:.4f}", f"{info.confidence:.3f}", info.note]
                               if info else ["", "", "", "", ""]), error])
            handle.flush()
            emit({"type": "file", "file": str(relative), "status": status,
                  "detail": detail, "message": error, **counts})
    result = {**counts, "cancelled": cancel_event.is_set(), "report": str(report_path),
              "output": str(destination), "elapsed": time.monotonic() - started}
    emit({"type": "finished", **result})
    return result


class SamplePackTuneToCApp:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText
        self.tk, self.ttk = tk, ttk
        self.root = tk.Tk()
        self.root.title("Sample Pack Tune To C")
        self.root.geometry("860x680")
        self.root.minsize(740, 620)
        self.root.configure(bg="#f4f6fa")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.closing = False
        self.last_output = None
        self.last_suggestion = ""
        self.input_path, self.output_path = tk.StringVar(), tk.StringVar()
        self.add_suffix, self.normalize = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Choose a sample folder to begin.")
        self.counter = tk.StringVar(value="Ready when you are")
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa", foreground="#1b263b", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"))
        style.configure("Muted.TLabel", foreground="#56647b")
        style.configure("Small.TLabel", font=("Segoe UI", 9), foreground="#56647b")
        style.configure("TButton", font=("Segoe UI", 10), padding=(14, 9))
        style.configure("Accent.TButton", background="#245bd6", foreground="white", font=("Segoe UI", 11, "bold"))
        style.map("Accent.TButton", background=[("disabled", "#aebbd1"), ("active", "#1748b0")])
        style.configure("TCheckbutton", background="#f4f6fa", font=("Segoe UI", 10), padding=(0, 5))
        style.configure("TEntry", padding=9, font=("Segoe UI", 10))
        style.configure("Horizontal.TProgressbar", background="#245bd6", troughcolor="#dfe5ef", thickness=9)
        body = ttk.Frame(self.root, padding=26)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(12, weight=1)
        ttk.Label(body, text="Sample Pack Tune To C", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="Turn a folder of WAVs into a new pack tuned to the nearest C octave.",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 2))
        ttk.Label(body, text="Your originals stay untouched. Subfolders are preserved.",
                  style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 20))
        self.edit_controls = []
        self._folder_row(body, 3, "1  Original sample folder", self.input_path, self.pick_input)
        self._folder_row(body, 5, "2  Save the C-tuned copy here", self.output_path, self.pick_output)
        ttk.Label(body, text="Use a new or empty folder outside the original pack.",
                  style="Small.TLabel").grid(row=7, column=0, sticky="w", pady=(4, 10))
        options = ttk.Frame(body)
        options.grid(row=8, column=0, sticky="ew")
        for label, variable in (("Add _C to filenames", self.add_suffix),
                                ("Normalize peak to −1 dB", self.normalize)):
            check = ttk.Checkbutton(options, text=label, variable=variable)
            check.pack(side="left", padx=(0, 26))
            self.edit_controls.append(check)
        actions = ttk.Frame(body)
        actions.grid(row=9, column=0, sticky="ew", pady=(14, 17))
        self.start_button = ttk.Button(actions, text="Create C-tuned pack", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="Open output folder", command=self.open_output, state="disabled")
        self.open_button.pack(side="right")
        self.edit_controls.append(self.start_button)
        progress = ttk.Frame(body)
        progress.grid(row=10, column=0, sticky="ew")
        ttk.Label(progress, textvariable=self.status, wraplength=770).pack(anchor="w")
        self.progress = ttk.Progressbar(progress, mode="determinate")
        self.progress.pack(fill="x", pady=(9, 6))
        ttk.Label(progress, textvariable=self.counter, style="Small.TLabel").pack(anchor="w")
        ttk.Label(body, text="Activity", style="Small.TLabel").grid(row=11, column=0, sticky="w", pady=(15, 5))
        self.log = ScrolledText(body, height=7, wrap="word", font=("Segoe UI", 9),
                                bg="white", fg="#334155", relief="flat", borderwidth=0,
                                padx=12, pady=10, state="disabled")
        self.log.grid(row=12, column=0, sticky="nsew")
        ttk.Label(body, text="Uncertain pitches are copied unchanged and flagged in the CSV report.",
                  style="Small.TLabel").grid(row=13, column=0, sticky="w", pady=(9, 0))
        self.input_path.trace_add("write", self.suggest_output)
        self.write_log("Choose your original pack, then create its C-tuned copy.")
        self.root.after(100, self.poll)

    def _folder_row(self, body, row, label, variable, command):
        self.ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=(0 if row == 3 else 14, 6))
        frame = self.ttk.Frame(body)
        frame.grid(row=row + 1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        entry = self.ttk.Entry(frame, textvariable=variable)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button = self.ttk.Button(frame, text="Browse…", command=command)
        button.grid(row=0, column=1)
        self.edit_controls.extend((entry, button))

    def suggest_output(self, *_):
        value = self.input_path.get().strip()
        if value:
            path = Path(value)
            suggestion = str(path.with_name(path.name + "_C")) if path.name else ""
            if not self.output_path.get().strip() or self.output_path.get() == self.last_suggestion:
                self.output_path.set(suggestion)
            self.last_suggestion = suggestion

    def pick_input(self):
        from tkinter import filedialog
        value = filedialog.askdirectory(parent=self.root, title="Choose the original sample pack", mustexist=True)
        if value:
            self.input_path.set(value)

    def pick_output(self):
        from tkinter import filedialog
        value = filedialog.askdirectory(parent=self.root, title="Choose or create an empty output folder", mustexist=False)
        if value:
            self.output_path.set(value)

    def write_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "101.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        from tkinter import messagebox
        try:
            source, destination = validate_paths(self.input_path.get(), self.output_path.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Check the folders", str(exc), parent=self.root)
            return
        options = (source, destination, "_C" if self.add_suffix.get() else "", self.normalize.get())
        self.running = True
        self.cancel_event.clear()
        self.last_output = None
        for control in self.edit_controls:
            control.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.progress.configure(value=0)
        self.status.set("Preparing your pack…")
        self.counter.set("Finding files")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.write_log(f"Original: {source}\nOutput: {destination}")
        threading.Thread(target=self.worker, args=options, daemon=False).start()

    def worker(self, source, destination, suffix, normalize):
        try:
            process_pack(source, destination, suffix, normalize, self.events.put, self.cancel_event)
        except Exception as exc:
            self.events.put({"type": "fatal", "message": str(exc) or type(exc).__name__,
                             "output": str(destination)})

    def cancel(self):
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("Stopping after the current file. Your partial pack and report will be kept.")

    def poll(self):
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event["type"]
            if kind == "status" and not self.cancel_event.is_set():
                self.status.set(event["message"])
            elif kind in ("started", "current", "file"):
                self.progress.configure(maximum=max(1, event["total"]), value=event["processed"])
                self.counter.set(f"{event['processed']} / {event['total']} WAVs   ·   "
                                 f"{event['tuned']} tuned   ·   {event['uncertain']} uncertain   ·   {event['errors']} errors")
                if kind == "current" and not self.cancel_event.is_set():
                    self.status.set(f"Tuning: {event['file']}")
                elif kind == "file":
                    self.write_log(f"{event['file']}  →  {event['detail']}" +
                                   (f"\n  {event['message']}" if event["message"] else ""))
            elif kind in ("finished", "fatal"):
                self.finish(event)
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(100, self.poll)

    def finish(self, event):
        from tkinter import messagebox
        self.running = False
        for control in self.edit_controls:
            control.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.last_output = event["output"] if Path(event["output"]).is_dir() else None
        self.open_button.configure(state="normal" if self.last_output else "disabled")
        if event["type"] == "fatal":
            self.status.set("Could not complete the pack.")
            self.write_log("Error: " + event["message"])
            if not self.closing:
                messagebox.showerror("Could not complete the pack", event["message"], parent=self.root)
            return
        prefix = "Stopped" if event["cancelled"] else "Finished"
        self.status.set(f"{prefix} · {event['processed']} of {event['total']} WAVs processed.")
        self.write_log(f"{prefix}. {event['tuned']} tuned, {event['uncertain']} uncertain, {event['errors']} errors.")
        if event["skipped_links"]:
            self.write_log(f"Skipped {event['skipped_links']} linked files or folders.")
        self.write_log(f"CSV report saved: {event['report']}")

    def open_output(self):
        from tkinter import messagebox
        try:
            if self.last_output:
                os.startfile(self.last_output)
        except OSError as exc:
            messagebox.showerror("Could not open folder", str(exc), parent=self.root)

    def close(self):
        if self.running:
            self.closing = True
            self.cancel()
            self.status.set("Finishing the current file and saving the report before closing…")
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-folder", default="")
    parser.add_argument("--output-folder", default="")
    parser.add_argument("--smoke-test", action="store_true")
    options = parser.parse_args()
    app = SamplePackTuneToCApp()
    if options.input_folder:
        app.input_path.set(options.input_folder)
    if options.output_folder:
        app.output_path.set(options.output_folder)
    if options.smoke_test:
        app.root.after(900, app.root.destroy)
    app.run()
