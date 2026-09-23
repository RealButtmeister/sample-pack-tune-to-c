# Sample Pack Tune To C

Create a separate copy of a WAV sample folder with each detected dominant pitch shifted to its nearest C octave. Folder structure is preserved; a CSV report records detection, shifts, and files copied unchanged. Originals remain untouched.

## Run

Install Python 3.11 or newer with Tkinter, then install the pinned dependencies:

```powershell
python -m pip install -r requirements.txt
python tools/sample_pack_tune_to_c_gui.py
```

On Windows, `Start Sample Pack Tune To C.cmd` starts the source GUI. Choose the source and a separate empty output folder. WAV subfolders are included. The default appends `_C`, writes tuned files as 24-bit PCM, and normalizes peaks to -1 dB; the window lets you disable the suffix and normalization.

Silent or uncertain files are copied unchanged. Cancellation completes the current file and writes a partial report. Results are estimates of dominant pitch, not proof of a sample's musical key; review the report and listen to the output.

For the command-line tuner:

```powershell
python tools/ostirus_tune_to_c.py "C:\Samples\Source" --output-dir "C:\Samples\Tuned"
```

Source development and packaging notes are in [BUILD.md](BUILD.md). Audio, generated reports, the packaged runtime, and executable builds are excluded from this repository.

## License

The existing [MIT License](LICENSE) is preserved unchanged. Installed dependencies retain their own licenses.
