#!/usr/bin/env python
"""
Tune a sample folder so each sample's dominant peak lands on a C note.

This makes an offline copy of the pack. It preserves the input folder layout,
adds _C to filenames by default, and writes a CSV report with detected source
frequency, target C frequency, and pitch-shift amount.
"""

from __future__ import annotations

import argparse
import csv
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from scipy.fft import rfft, rfftfreq


C0_HZ = 16.351597831287414


@dataclass
class TuneInfo:
    source_hz: float
    target_hz: float
    semitones: float
    confidence: float
    note: str
    message: str = ""


def c_frequency(octave: int) -> float:
    return C0_HZ * (2.0**octave)


def nearest_c(freq_hz: float) -> tuple[float, str]:
    if freq_hz <= 0:
        return 0.0, "unknown"
    octave = round(math.log2(freq_hz / C0_HZ))
    octave = max(0, min(10, octave))
    return c_frequency(octave), f"C{octave}"


def active_region(mono: np.ndarray, sample_rate: int, max_sec: float) -> np.ndarray:
    if len(mono) == 0:
        return mono
    env = np.abs(mono)
    peak = float(np.max(env))
    if peak <= 0:
        return mono[: min(len(mono), int(sample_rate * max_sec))]
    active = np.flatnonzero(env >= peak * 0.04)
    if len(active) == 0:
        start = 0
    else:
        start = max(0, int(active[0]) - int(sample_rate * 0.01))
    end = min(len(mono), start + int(sample_rate * max_sec))
    return mono[start:end]


def analysis_min_hz(path: Path, default_min_hz: float) -> float:
    name = str(path).lower()
    if "drum_hat" in name or "hat" in name:
        return 1200.0
    if "cymbal" in name or "noise_or_riser" in name:
        return 700.0
    if "snare" in name or "clap" in name:
        return 120.0
    if "kick" in name or "bass" in name:
        return 24.0
    return default_min_hz


def detect_peak_frequency(audio: np.ndarray, sample_rate: int, max_analysis_sec: float, min_hz: float) -> tuple[float, float]:
    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    segment = active_region(mono.astype(np.float32, copy=False), sample_rate, max_analysis_sec)
    if len(segment) < 128:
        return 0.0, 0.0

    segment = segment - float(np.mean(segment))
    window = np.hanning(len(segment)).astype(np.float32)
    mag = np.abs(rfft(segment * window)) + 1e-12
    freqs = rfftfreq(len(segment), 1.0 / sample_rate)

    mask = (freqs >= min_hz) & (freqs <= min(18000.0, sample_rate * 0.46))
    if not np.any(mask):
        return 0.0, 0.0

    usable_freqs = freqs[mask]
    usable_mag = mag[mask]

    # Slightly favor perceptually meaningful peaks over DC/rumble while still
    # allowing kicks to lock to low C notes.
    weight = np.clip(usable_freqs / 55.0, 0.55, 3.0) ** 0.22
    weighted = usable_mag * weight
    idx = int(np.argmax(weighted))
    source_hz = float(usable_freqs[idx])

    sorted_mag = np.sort(weighted)
    floor = float(np.median(sorted_mag) + 1e-12)
    confidence = float(weighted[idx] / floor)
    return source_hz, confidence


def pitch_shift_audio(audio: np.ndarray, sample_rate: int, semitones: float) -> np.ndarray:
    if abs(semitones) < 0.01:
        return np.array(audio, copy=True)
    channels = []
    for ch in range(audio.shape[1]):
        shifted = librosa.effects.pitch_shift(y=audio[:, ch].astype(np.float32), sr=sample_rate, n_steps=semitones)
        channels.append(shifted.astype(np.float32))
    min_len = min(len(ch) for ch in channels)
    return np.stack([ch[:min_len] for ch in channels], axis=1)


def normalize_peak(audio: np.ndarray, target_db: float) -> np.ndarray:
    target = 10.0 ** (target_db / 20.0)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak <= 0:
        return audio
    return audio * min(16.0, target / peak)


def copy_unprocessed(path: Path, output_path: Path, note: str, message: str) -> TuneInfo:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(path), str(output_path))
    print(f"WARNING: copied unchanged ({note}): {path}")
    if message:
        print(f"         {message}")
    return TuneInfo(0.0, 0.0, 0.0, 0.0, note, message)


def tune_file(path: Path, output_path: Path, args: argparse.Namespace) -> TuneInfo:
    try:
        audio, sample_rate = sf.read(str(path), always_2d=True, dtype="float32")
        source_hz, confidence = detect_peak_frequency(audio, sample_rate, args.max_analysis_sec, analysis_min_hz(path, args.min_hz))
        target_hz, note = nearest_c(source_hz)

        if source_hz <= 0 or target_hz <= 0 or confidence < args.min_confidence:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(output_path))
            return TuneInfo(source_hz, target_hz, 0.0, confidence, "copied_low_confidence")

        semitones = 12.0 * math.log2(target_hz / source_hz)
        if abs(semitones) > args.max_shift_semitones:
            semitones = max(-args.max_shift_semitones, min(args.max_shift_semitones, semitones))
            target_hz = source_hz * (2.0 ** (semitones / 12.0))
            note = "C_clamped"

        tuned = pitch_shift_audio(audio, sample_rate, semitones)
        if args.normalize_peak_db is not None:
            tuned = normalize_peak(tuned, args.normalize_peak_db)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(output_path), tuned, sample_rate, subtype="PCM_24")
        return TuneInfo(source_hz, target_hz, semitones, confidence, note)
    except Exception as exc:
        return copy_unprocessed(path, output_path, "copied_processing_error", str(exc))


def output_name(path: Path, suffix: str) -> str:
    return f"{path.stem}{suffix}{path.suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, required=True)
    parser.add_argument("--suffix", default="_C")
    parser.add_argument("--max-analysis-sec", type=float, default=1.0)
    parser.add_argument("--min-hz", type=float, default=24.0)
    parser.add_argument("--min-confidence", type=float, default=4.0)
    parser.add_argument("--max-shift-semitones", type=float, default=6.0)
    parser.add_argument("--normalize-peak-db", type=float, default=-1.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    wavs = sorted(p for p in args.input_dir.rglob("*.wav") if p.is_file())
    if not wavs:
        raise SystemExit(f"No WAV files found in {args.input_dir}")

    rows: list[list[str]] = []
    shifted = 0
    copied = 0
    for wav in wavs:
        rel = wav.relative_to(args.input_dir)
        dest = args.output_dir / rel.parent / output_name(wav, args.suffix)
        if args.dry_run:
            try:
                audio, sample_rate = sf.read(str(wav), always_2d=True, dtype="float32")
                source_hz, confidence = detect_peak_frequency(audio, sample_rate, args.max_analysis_sec, analysis_min_hz(wav, args.min_hz))
                target_hz, note = nearest_c(source_hz)
                semitones = 12.0 * math.log2(target_hz / source_hz) if source_hz > 0 and target_hz > 0 else 0.0
                info = TuneInfo(source_hz, target_hz, semitones, confidence, note)
            except Exception as exc:
                info = TuneInfo(0.0, 0.0, 0.0, 0.0, "copied_processing_error", str(exc))
                print(f"WARNING: would copy unchanged (copied_processing_error): {wav}")
                print(f"         {exc}")
        else:
            info = tune_file(wav, dest, args)

        if abs(info.semitones) >= 0.01:
            shifted += 1
        else:
            copied += 1
        rows.append(
            [
                str(rel),
                str(dest.relative_to(args.output_dir)),
                f"{info.source_hz:.3f}",
                f"{info.target_hz:.3f}",
                f"{info.semitones:.4f}",
                f"{info.confidence:.3f}",
                info.note,
                info.message,
            ]
        )

    print(f"files: {len(wavs)}")
    print(f"shifted: {shifted}")
    print(f"copied/no shift: {copied}")

    if not args.dry_run:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        report = args.output_dir / "tune_to_c_report.csv"
        with report.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["source_file", "tuned_file", "source_peak_hz", "target_c_hz", "semitones", "confidence", "target_note", "message"])
            writer.writerows(rows)
        print(f"wrote: {args.output_dir}")
        print(f"report: {report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
