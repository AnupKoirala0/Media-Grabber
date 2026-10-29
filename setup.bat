@echo off
python --version >nul 2>&1
if errorlevel 1 (
   winget install python
python -v
python -m pip install yt-dlp
exit
    
) else (
    python -m pip install yt-dlp
exit
)

