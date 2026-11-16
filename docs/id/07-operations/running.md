# Menjalankan sehari-hari

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/07-operations/running.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

## Perintah

```bash
./run.sh all          # semuanya + dashboard di browser, :8081
./run.sh headless     # semuanya, tanpa UI: cara robot dikirim
./run.sh slint        # semuanya + panel di layar robot sendiri
./run.sh status       # apa yang berjalan, dan apa yang dimuatnya
./run.sh model        # LLM / model pendengar / suara: dipilih, dikonfigurasi, berjalan
./run.sh info         # versi panjang
./run.sh stop
```

Ketiga mode hanya berbeda pada **wajah mana yang dinyalakan**. Alur suaranya sama, karena kedua layar
bersifat opsional dan merupakan klien mandiri `robot-core`: "galat di UI tidak boleh menghentikan robot".

| Mode | Menambah | Biaya | Pakai saat |
|---|---|---|---|
| `all` | `robot-ui`, dashboard | sekitar 60 MiB | menyiapkan atau men-debug |
| `headless` | tidak ada | 0 | cara robot seharusnya berjalan: ajak bicara. Atur sekali dari `all`, pengaturan tersimpan |
| `slint` | `robot-face`, panel di layar yang terpasang | sekitar 11 MiB, plus 8,4 MiB buffer layar | ada layar terpasang |

### Opsi yang diteruskan

| Opsi | Efek |
|---|---|
| `--check` | Cetak konfigurasi yang akan dipakai, **termasuk pin dashboard**, tanpa menyalakan apa pun |
| `--sim` | Nyalakan juga **tubuh simulasi** (`tools/stm32_sim`) |
| `--no-asr` | Lewati mikrofon |
| `--no-embed` | Lewati server embedding |
| `--no-vision` | Lewati kamera sekali ini. `VISION_ENABLED=0` di `config/vision.env` menghapusnya selamanya |
| `--ctx N` | Ganti ukuran konteks LLM untuk satu kali jalan |
| `--wav FILE` | Beri rekaman sebagai pengganti mikrofon |

Satu layanan sekali jalan (yang juga dipakai tombol di halaman Services):

```bash
./scripts/run_stack.sh svc start|stop|restart llm|embed|tts|core|asr|body|ui|face
```

## Ke mana melihat

| Apa | Di mana |
|---|---|
| Log semua layanan di bawah `./run.sh` | `.cache/tmp/`: `core.log`, `llama-server.log`, `asr.log`, `tts.log`, `embed.log`, `vision.log`, `ui.log`, `stm32_sim.log` |
| Log di bawah systemd | `journalctl -u robot-core -f` (butuh jurnal tetap: `setup_logging.sh`) |
| Percakapan saat berlangsung | `tail -f .cache/tmp/core.log` |
| Apa yang dimuat, di GPU atau CPU | `./scripts/measure/whats_running.py` |
| Langsung, di browser | halaman **Services**, **Monitor**, dan **Logs** |
| Semua yang disimpan dashboard | `run/settings.json`, dan `./run.sh <mode> --check` |

`people.db` dan `faces.db` adalah **data pribadi** (percakapan dan wajah) dan tetap di robot. Jangan
disalin keluar dan jangan di-commit (`run/` diabaikan git).

### Jawaban satu baris untuk "apa yang terjadi?"

| Pertanyaan | Perintah |
|---|---|
| Mode pencarian apa yang dipakai? (pencarian kata kunci diam-diam gagal untuk pertanyaan berbahasa lain) | `grep 'robot-rag: retrieved' .cache/tmp/core.log` menjawab `by vector` atau `by keyword` |
| Mengapa sebuah ucapan dibuang, atau mengapa robot jadi tuli? | `grep 'robot-asr: dropped' .cache/tmp/asr.log` |
| Bahasa apa yang diputuskan, dan mengapa? | `grep 'recogniser said' .cache/tmp/core.log` |
| Port mana tubuh, dan apakah penemuan berhasil? | `grep 'robot-core: microcontroller' .cache/tmp/core.log` |
| Apakah tubuh menerima JSON fase? | `grep 'robot-serial: reply' .cache/tmp/core.log` |
| Apakah keepalive menyalakan ulang sesuatu? | `grep 'is down; restarting' .cache/tmp/core.log` |

## Merawatnya

| Tugas | Perintah | Artinya |
|---|---|---|
| Apakah memori sehat? | `./scripts/measure/whats_running.py` | RSS dan **swap** per layanan. Swap bukan nol pada `tts` berarti suara GPU tak lagi yang tercepat |
| Seberapa cepat menjawab? | `./scripts/measure/voice_latency.py --wav <rekaman> -n 5` | Dari akhir bicara sampai kata pertama. Median 2181 ms, terbaik 1554 ms terukur; target 1500 ms |
| Apakah pengetahuannya bekerja? | `./scripts/measure/knowledge_qa.py` | Apakah ia tahu yang dimasukkan, dan seberapa cepat |
| Apakah jalur kegagalan bertahan? | `python scripts/hardening/failure_drills.py` | Mematikan tiap layanan sungguhan. 24 dari 24 lulus |
| Apakah bertahan lama? | `python scripts/hardening/soak.py --minutes 60` | Soak 45 menit bersih; **24 jam belum pernah dijalankan** |
| Apakah tubuh ada? | Config → Hardware → *Ping all ports* | Port mana yang menjawab `{PING_robot}`, dan secepat apa |

## Aturan praktis

* **Periksa swap sebelum memercayai angka latensi apa pun.**
* **Atur dari `all`, kirim dengan `headless`.** Pengaturan tersimpan.
* **Pilih `/dev/serial/by-id/...` daripada `ttyACM0`.** Penemuan menyimpan nama yang stabil untuk Anda.
* **Jangan jalankan `pkill -f <pola>` dari shell yang baris perintahnya memuat pola itu**: shell ikut mati.
* **Setelah mengubah layanan, baca perubahannya di bawah kedua pengawas**: `run_stack.sh` dan unit.
