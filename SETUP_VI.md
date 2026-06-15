# Hướng dẫn chạy & chấm điểm (môi trường máy này)

> Máy này là Ubuntu 20.04 (glibc 2.31) nhưng binary cần glibc ≥ 2.38, nên ta chạy
> binary trong Docker `python:3.12-slim`. LLM dùng **OmniRoute** (giống Day03/04/09/11).
> Mọi cấu hình bí mật nằm trong `.env` (đã gitignore).

## 1. Yêu cầu
- Docker đang chạy; OmniRoute sống ở `localhost:20128` (đã kiểm tra).
- `.env` chứa: `OMNI_API_KEY`, `OMNI_BASE_URL`, `AGENT_MODEL`,
  và `LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST` (Langfuse Cloud).

## 2. Chạy mô phỏng (sim) → tạo run_output.json
```bash
./run.sh                                  # practice  -> runs/practice.json
PHASE=public ./run.sh                     # public    -> runs/public.json
OUT=run_output.json PHASE=public ./run.sh # bản NỘP ở gốc repo (SUBMIT.md yêu cầu)
```
- Đổi model: `AGENT_MODEL=claude/claude-sonnet-4-6 ./run.sh`
- Tham số sim truyền thẳng: `./run.sh --users 200 --turns 12 --concurrency 12`

## 3. Quan sát bằng Langfuse Cloud (tùy chọn)
```bash
LANGFUSE=1 ./run.sh        # chạy sim (ghi telemetry ra logs/) rồi đẩy lên Langfuse Cloud
```
Vì sao tách 2 bước: stack OpenTelemetry/requests của Langfuse KHÔNG import được bên
trong interpreter đông-lạnh (PyInstaller cắt bớt stdlib). Nên wrapper ghi telemetry
ra `logs/*.log` (backend file, chạy ổn trong tiến trình), rồi
`tools/export_to_langfuse.py` (Python thường) đọc và đẩy lên Langfuse Cloud — đúng
best-practice: observability không nằm trên đường đi chính của agent.

## 4. Tự chấm điểm (ƯỚC LƯỢNG, trước khi có file chấm của thầy)
```bash
WRAPPER=harness/eval_wrapper.py ./run.sh            # ghi runs/eval.jsonl (đủ trace)
python harness/selfscore.py --eval runs/eval.jsonl --out runs/score_estimate.json
```
`selfscore.py` lặp lại công thức trong RULES.md:
`100×(0.32·correct + 0.16·quality + 0.13·error + 0.08·latency + 0.09·cost +
0.07·drift + 0.15·prompt) + tối đa 22·diagnosis-F1`.
- `correct` so với tổng tính lại từ **chính dữ liệu tool** của agent (giá/giảm/ship
  do sim trả về = ground-truth cục bộ).
- `quality` cần LLM-judge → để N/A (báo 2 mức: bỏ quality, và quality≈correct).
- `diagnosis_f1` cần đáp án lỗi của thầy → không tự chấm được.
→ Đây chỉ là **ước lượng để validate hướng đi**, không phải điểm thật.

## 5. Chấm điểm CHÍNH THỨC (khi thầy phát hành)
Thầy sẽ gửi **binary `observathon-score`** (chưa có trong `bin/`). Nó mới là thứ
sinh ra `score.json` thật:
```bash
./bin/<phase>/observathon-score --run run_output.json \
    --findings solution/findings.json --team <TEAM> --out score.json
```
`score.json` chứa **headline 0–100** + breakdown từng tiêu chí. `runs/score_estimate.json`
do `selfscore.py` tạo ra chỉ là **bản xem trước cấu trúc** (có cờ `"estimated": true`).

## 6. Nộp bài
```bash
python harness/selfcheck.py          # phải PASS hết
git add solution/ run.sh harness/ tools/ run_output.json score.json
git commit -m "<team> <phase>" && git push
```

## Các file đã thêm/sửa
- `solution/config.json` · `solution/prompt.txt` · `solution/wrapper.py` ·
  `solution/findings.json` · `solution/notes.md` — bài nộp.
- `run.sh` — chạy sim qua Docker+OmniRoute (+Langfuse).
- `tools/export_to_langfuse.py` — đẩy telemetry lên Langfuse Cloud.
- `harness/eval_wrapper.py` + `harness/selfscore.py` — tự chấm điểm ước lượng.
- `.env` (gitignore) — khóa OmniRoute + Langfuse.
