@echo off
title Start MediaMTX and Sony Stream

echo Starting MediaMTX...
start "" "D:\Driver\mediamtx_v1.18.1_windows_amd64\mediamtx.exe"

echo Waiting 5 seconds...
timeout /t 5 /nobreak > nul

echo Starting Stream Camera Sony...
start "" "D:\Camera\Dev\Stream_camera\Stream_camera_rtsp\output\Stream_camera_Sony.exe"

echo Done.
pause