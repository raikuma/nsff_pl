#!/bin/bash
#ROOT_DIR=/workspace/data/Balloon1
NUM_GPUS=${1:-1}
python train.py \
  --dataset_name monocular --root_dir $ROOT_DIR \
  --img_wh 480 270 --start_end 0 12 \
  --N_samples 128 --N_importance 0 --encode_t --use_viewdir \
  --num_epochs 200 --batch_size 512 \
  --optimizer adam --lr 5e-4 --lr_scheduler cosine \
  --exp_name exp \
  --num_gpus $NUM_GPUS &&
python eval.py \
  --dataset_name monocular --root_dir $ROOT_DIR \
  --N_samples 128 --N_importance 0 --img_wh 480 270 --start_end 0 12 \
  --encode_t --output_transient \
  --split test_fixview0_interp0 --video_format gif --fps 5 \
  --ckpt_path /workspace/ckpts/exp/epoch=199.ckpt --scene_name Balloon1_reconstruction \
  --use_viewdir