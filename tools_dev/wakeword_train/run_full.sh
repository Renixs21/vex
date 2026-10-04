#!/bin/bash
cd /c/Users/sanja/wakeword_train
export PATH="$PATH:/c/Program Files/eSpeak NG" PYTHONUTF8=1 PYTHONIOENCODING=utf-8
echo "[$(date +%T)] generate"; ./lk/Scripts/livekit-wakeword.exe generate configs/hey_vex_full.yaml > full_gen.txt 2>&1 || exit 1
echo "[$(date +%T)] mix"; ./lk/Scripts/python.exe mix_real_full.py || exit 1
echo "[$(date +%T)] augment"; ./lk/Scripts/livekit-wakeword.exe augment configs/hey_vex_full.yaml > full_aug.txt 2>&1
echo "[$(date +%T)] extract"; ./lk/Scripts/python.exe extract_run_full.py > full_ext.txt 2>&1 || exit 1
echo "[$(date +%T)] train"; ./lk/Scripts/python.exe -u train_run.py configs/hey_vex_full.yaml > full_train.txt 2>&1 || exit 1
echo "[$(date +%T)] export"; ./lk/Scripts/livekit-wakeword.exe export configs/hey_vex_full.yaml > full_export.txt 2>&1
echo "[$(date +%T)] ALL DONE"
