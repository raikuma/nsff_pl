#!/bin/bash
wget -O third_party/depth/weights/dpt_large-midas-2f21e586.pt https://github.com/intel-isl/DPT/releases/download/1_0/dpt_large-midas-2f21e586.pt &&
mkdir third_party/flow/models &&
wget --load-cookies ~/cookies.txt "https://docs.google.com/uc?export=download&confirm=$(wget --quiet --save-cookies ~/cookies.txt --keep-session-cookies --no-check-certificate 'https://docs.google.com/uc?export=download&id=1MqDajR89k-xLV0HIrmJ0k-n8ZpG6_suM' -O- | sed -rn 's/.*confirm=([0-9A-Za-z_]+).*/\1\n/p')&id=1MqDajR89k-xLV0HIrmJ0k-n8ZpG6_suM" -O third_party/flow/models/raft-things.pth && rm -rf ~/cookies.txt
