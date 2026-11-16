# Memasang di papan baru

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/07-operations/install.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

Urutannya penting, dan sebagian besar langkah aman diulang. Perintah yang mengubah sistem memakai
`sudo`. Tidak satu pun dijalankan oleh buku panduan ini.

## 1. Periksa papan (hanya membaca)

```bash
./scripts/bringup/check_board.sh
```

Kode keluar 0 berarti prasyarat terpenuhi. Yang diperiksa:

| Pemeriksaan | Mengapa |
|---|---|
| JetPack **6.x** | `TECH_STACK.md` mengunci lini 6.x (JetPack 7 ditunda) |
| Mode daya **`MAXN_SUPER`** | perbaiki dengan `sudo nvpmodel -m 2` |
| Target bawaan `multi-user.target`, **tanpa desktop** | desktop memakan sekitar 600 MB anggaran (batasan keras 5) |
| `jetson-stats` dan `jtop` dapat diimpor | halaman Monitor dan pengukuran memori membacanya |
| 20 GB atau lebih kosong untuk model; root 10 GB atau lebih (peringatan di bawah itu) | model beberapa GB; root hampir penuh |
| Mikrofon dan speaker USB, dan `PULSE_SOURCE` serta `PULSE_SINK` **dikunci** | `pulse` dan `default` mengikuti bawaan PulseAudio, yang di sini adalah codec onboard tanpa sambungan: rekaman senyap dan suara tidak ke mana-mana, **keduanya melaporkan berhasil**. Ini sudah menghabiskan dua sesi debugging |
| Tubuh: `ttyACM*` dengan vendor USB `0483`, `2341`, atau `2e8a` | lihat catatan di bawah |
| Setiap unit menyatakan `Restart=on-failure`; jurnal bersifat tetap | OOM kill pada 8 GB adalah soal "kapan", bukan "apakah" |

> **Pemeriksaan tubuh tidak mengenal ESP32.** Ia hanya melihat `ttyACM*` dan hanya tiga vendor itu.
> Tubuh di balik chip USB-UART CP2102 (`10c4`) atau CH340 (`1a86`) muncul sebagai `ttyUSB*` dan akan
> dilaporkan "no STM32". Itu batas pemeriksaannya, bukan kerusakan robot: penemuan dengan
> `{PING_robot}` tetap menemukan tubuh. Masalah Diketahui #19.

## 2. Jaga semuanya di luar disk root

```bash
source scripts/bringup/env.sh
```

Mengarahkan `PIP_CACHE_DIR`, `TMPDIR`, `HF_HOME`, dan `ROBOT_MODELS` ke bawah repo, menambahkan indeks
paket Jetson NVIDIA, dan mengaktifkan `.venv`. **Jalankan di setiap shell baru sebelum memasang atau
mengunduh.**

## 3. Unduh model

```bash
./scripts/models/fetch_llm.sh          # sekitar 2,4 GB
./scripts/models/fetch_whisper.sh      # sekitar 470 MB, pengurai suara
./scripts/models/fetch_tts.sh          # suara-suara
./scripts/models/fetch_vision.sh       # sekitar 57 MiB, hanya jika ada kamera
```

Model tambahan nanti:

```bash
./scripts/models/fetch_hf.sh <repo> <file>    # atau tempel URL huggingface.co
./scripts/models/fetch_hf.sh --dry-run ...    # akan disimpan di mana?
./scripts/models/list.sh                      # apa yang terpasang, dan biayanya
```

Tujuan penyimpanan ditebak dari nama berkas, dan tujuan itulah yang menentukan apakah robot dapat
melihat berkasnya: `.gguf` ke `models/llm/`, `ggml-*.bin` ke `models/asr/whisper/`, `.onnx` ke
`models/tts/piper/`. **Ia muncul di halaman Memory dalam sekitar 3 detik, tanpa menyalakan ulang.**
Memasang tidak berarti memilih: halaman Memory menghitung dulu biayanya terhadap batas 7620 MiB.

## 4. Atur rahasia

```bash
cp .env.example .env
```

Lalu isi `ROBOT_UI_PASSWORD` (dashboard **tidak mau menyala** tanpanya) dan `ROBOT_UI_STORAGE_SECRET`,
yang dibuat dengan `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Tambahkan juga
`CORE_WS_TOKEN` ke `.env`: dibaca oleh `robot-core`, `robot-ui`, dan `robot-face`, dan **`.env.example`
tidak mencantumkannya** ❓. `.env` diabaikan git. Tidak ada rahasia di berkas yang dilacak.

## 5. Lihat dulu sebelum menyalakan

```bash
./run.sh headless --check
```

Mencetak konfigurasi yang akan dipakai, **termasuk yang disematkan (pin) dari dashboard**, dan tidak
menyalakan apa pun. Pin mengalahkan berkas env, jadi di sinilah pin usang terlihat.

## 6. Nyalakan

```bash
./run.sh all                 # semuanya dan dashboard di :8081
```

Lihat [Menjalankan sehari-hari](running.md). Urutan menyalakan itulah yang membuat *cold start*
berhasil: LLM dulu, ditunggu sampai benar-benar siap, baru whisper.

## 7. Untuk robot yang dibiarkan menyala (produksi)

```bash
sudo ./scripts/install_services.sh         # menautkan unit ke /etc/systemd/system
sudo ./scripts/bringup/setup_sudoers.sh    # start/stop/restart terbatas untuk unit robot
sudo ./scripts/bringup/setup_logging.sh    # jurnal tetap, dibatasi 200 MB
sudo ./scripts/bringup/setup_udev.sh       # /dev/robot-stm32 (hanya cocok dengan STMicroelectronics)
sudo loginctl enable-linger jetson         # agar /run/user/1000 (PulseAudio) ada saat boot
sudo systemctl enable --now robot.target
python scripts/hardening/failure_drills.py
```

Penjelasan tiap langkah ada di [versi Inggris](../../en/07-operations/install.md#7-for-a-robot-that-is-left-running-production).
Pengguna harus ada di grup `dialout` untuk membuka port serial tubuh.

> **Belum pernah di-boot.** Tidak satu pun langkah di bagian 7 pernah dijalankan di papan ini. Siap-siap
> bertemu sesuatu. Jika terjadi, tambahkan ke [`TROUBLESHOOTING.md`](../../../dev-docs/ai/TROUBLESHOOTING.md)
> beserta uji regresi.
