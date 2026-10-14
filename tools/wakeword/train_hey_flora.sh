#!/usr/bin/env bash
# Train FLORA's "Hey Flora" openWakeWord model on a CUDA GPU machine
# (built for a RunPod PyTorch pod). Each stage leaves a marker in $WORK and
# is skipped on a rerun, so a failed stage can be fixed and the script rerun.
# Result: $WORK/model/hey_flora.onnx
set -euo pipefail
WORK=${WORK:-/workspace/wakeword}
mkdir -p "$WORK" && cd "$WORK"
done_stage() { [ -f ".done_$1" ]; }
mark() { touch ".done_$1"; echo "=== stage $1 done $(date +%T)"; }

if ! done_stage env; then
  # Python 3.11: piper-phonemize has no wheels for newer versions.
  uv venv -q --python 3.11 venv
  . venv/bin/activate
  uv pip install -q "torch==2.1.2" "torchaudio==2.1.2" --index-url https://download.pytorch.org/whl/cu121
  uv pip install -q "numpy==1.26.4" "scipy==1.11.4" piper-phonemize webrtcvad \
    "mutagen==1.47.0" "torchinfo==1.8.0" "torchmetrics==1.2.0" "speechbrain==0.5.14" \
    "audiomentations==0.33.0" "torch-audiomentations==0.11.0" "acoustics==0.2.6" \
    "pronouncing==0.2.0" "datasets==2.14.6" "pyarrow==14.0.2" librosa "deep-phonemizer==0.0.19" \
    "onnxruntime==1.17.3" onnx pyyaml tqdm soundfile "huggingface_hub<0.25"
  [ -d openWakeWord ] || git clone -q https://github.com/dscripka/openWakeWord
  uv pip install -q --no-deps -e ./openWakeWord
  python -c "import openwakeword.utils as u; u.download_models()"
  mark env
fi
. venv/bin/activate

if ! done_stage piper; then
  [ -d piper-sample-generator ] || git clone -q https://github.com/rhasspy/piper-sample-generator
  git -C piper-sample-generator checkout -q v2.0.0
  wget -q -O piper-sample-generator/models/en_US-libritts_r-medium.pt \
    https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt
  mark piper
fi

if ! done_stage features; then
  # Precomputed openWakeWord features: ~2000 h of general speech/noise for
  # negatives, and a validation set for measuring false accepts per hour.
  base=https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main
  wget -q -c "$base/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
  wget -q -c "$base/validation_set_features.npy"
  mark features
fi

if ! done_stage audio; then
  # Its own environment: these datasets need a newer `datasets` than openWakeWord.
  [ -d dlvenv ] || { uv venv -q --python 3.11 dlvenv &&
    VIRTUAL_ENV=$WORK/dlvenv uv pip install -q "datasets>=3,<4" soundfile librosa "numpy<2.3"; }
  dlvenv/bin/python - <<'EOF'
import os, numpy as np, scipy.io.wavfile, datasets
from tqdm import tqdm

def save(dataset, folder, limit=None):
    os.makedirs(folder, exist_ok=True)
    for n, row in enumerate(tqdm(dataset, desc=folder, total=limit)):
        if limit and n >= limit:
            break
        audio = row["audio"]["array"]
        scipy.io.wavfile.write(f"{folder}/{n}.wav", 16000, (np.clip(audio, -1, 1) * 32767).astype(np.int16))

# Room impulse responses, so clips sound like a real room.
rir = datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True)
save(rir.cast_column("audio", datasets.Audio(sampling_rate=16000)), "mit_rirs")
# Background noise, music, TV and household sound mixed under the clips (~25 h).
audioset = datasets.load_dataset("agkphysics/AudioSet", "balanced", split="train", streaming=True)
save(audioset.cast_column("audio", datasets.Audio(sampling_rate=16000)), "background/audioset", 9000)
EOF
  mark audio
fi

if ! done_stage config; then
  python - <<'EOF'
import yaml
config = yaml.safe_load(open("openWakeWord/examples/custom_model.yml"))
config.update({
    "model_name": "hey_flora",
    "target_phrase": ["hey flora"],
    # Similar-sounding phrases that must not wake her.
    "custom_negative_phrases": ["hey laura", "hey nora", "hey dora", "hey cora", "hey florida",
                                "hey flower", "a flora", "hey for a"],
    "n_samples": 30000,
    "n_samples_val": 3000,
    "tts_batch_size": 100,
    "augmentation_batch_size": 32,
    "piper_sample_generator_path": "./piper-sample-generator",
    "output_dir": "./model",
    "rir_paths": ["./mit_rirs"],
    "background_paths": ["./background/audioset"],
    "background_paths_duplication_rate": [1],
    "false_positive_validation_data_path": "./validation_set_features.npy",
    "augmentation_rounds": 1,
    "feature_data_files": {"ACAV100M_sample": "./openwakeword_features_ACAV100M_2000_hrs_16bit.npy"},
    "batch_n_per_class": {"ACAV100M_sample": 1024, "adversarial_negative": 70, "positive": 70},
    "steps": 50000,
    "max_negative_weight": 1500,
    "target_false_positives_per_hour": 0.2,
})
yaml.safe_dump(config, open("hey_flora.yml", "w"))
print(yaml.safe_dump(config))
EOF
  mark config
fi

train=openWakeWord/openwakeword/train.py
# Training's data loader workers share batches through file handles; the
# container's default limit (1024) runs out within seconds.
ulimit -n "$(ulimit -Hn)"
for stage in generate_clips augment_clips train_model; do
  if ! done_stage "$stage"; then
    if [ "$stage" = train_model ]; then
      # Training ends with an optional TensorFlow Lite conversion that needs
      # onnx_tf; FLORA uses the ONNX model, so its presence means success.
      python "$train" --training_config hey_flora.yml --train_model || [ -f model/hey_flora.onnx ]
    else
      python "$train" --training_config hey_flora.yml "--$stage"
    fi
    mark "$stage"
  fi
done
ls -la model/
echo "=== ALL DONE"
