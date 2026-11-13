# WildClawBench Gym execution snapshot

This MIT-licensed derivative of [internlm/WildClawBench](https://github.com/internlm/WildClawBench)
contains the original 60 tasks, skills and native OpenClaw executor, with gateway
readiness/isolation, bounded process cleanup, compaction and LF-only transcript
compatibility fixes. It is the source dependency for the evaluation bridge in
[NVIDIA-NeMo/Gym PR #6015](https://github.com/NVIDIA-NeMo/Gym/pull/6015).
It retains original task rubrics and actor instructions. Its task revision
predates upstream November 4/5 updates, so scores are version-specific.

This repository contains text source only. Task fixtures, images, model weights,
cookies, credentials, generated outputs and private Git history are excluded.
No fresh leaderboard reproduction is claimed.

## Assets and native worker

Follow the public upstream README for the official
[Hugging Face assets](https://huggingface.co/datasets/internlm/WildClawBench).
Place task fixtures in `workspace/` and the official container tar in
`Images/wildclawbench-ubuntu_v1.3.tar`. Review `script/prepare.sh` and run it to
prepare videos, paper assets, safety repositories and SAM3 weights; some downloads
require your own authenticated access. Do not commit cookies or downloaded data.
The minimal Gym native worker needs Python 3.11+, python-dotenv, pyyaml and
`tokenizers>=0.20,<0.23`; `requirements.txt` describes the broader native runtime.

For Docker, load the tar and tag it `wildclawbench-ubuntu:v1.3`. For Enroot, use a
site-approved conversion of that Docker image and put the resulting SQSH at
`.cache/wildclawbench-ubuntu_v1.3.sqsh`. Enroot shares host PID/network namespaces;
use a dedicated trusted allocation, not a shared-host security boundary.
The Gym benchmark README supplies preparation, run and scoring instructions.

## License

MIT; see LICENSE. Original benchmark and skill notices are retained.
