# Source setup and optional packaging

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python tools/sample_pack_tune_to_c_gui.py
```

The `tools` modules must stay together. The GUI and CLI load the same pitch-processing engine. Tkinter comes with a full Python installation; it is not a pip package.

Validate syntax without reading any audio:

```powershell
python -m compileall -q tools
python tools/ostirus_tune_to_c.py --help
```

No native compilation is required. A separately created executable must include the numerical/audio dependencies and their licenses; the former packaged `_internal` directory is intentionally excluded. No standalone binary build is reproduced by these source staging checks.
