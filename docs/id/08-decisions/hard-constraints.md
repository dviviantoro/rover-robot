# Batasan keras

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/08-decisions/hard-constraints.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

Delapan aturan di [`AGENTS.md`](../../../AGENTS.md) hanya boleh dilanggar dengan persetujuan manusia. Singkat karena mahal: tiap
aturan dibeli dengan kegagalan atau pengukuran. **Jika sebuah tugas tampak membutuhkan salah satunya, berhenti dan tanya.
Jangan mencari jalan memutar.**

| # | Batasan | Mengapa, dan buktinya | Biayanya |
|---|---|---|---|
| 1 | **Total RAM kondisi stabil paling banyak sekitar 8 GB, targetnya 7 GB** | Orin Nano punya memori **terpadu**, dipakai CPU dan GPU bersama, dan **7620 MiB** yang dapat dipakai. Melampaui berarti OOM kill, bukan melambat. Tumpukan penuh mencapai sekitar **6,5 GB** | Setiap ketergantungan baru harus dihitung dulu di `memory-budget.md` |
| 2 | **Satu LLM resident sekali waktu** | Dua model bahasa tidak muat. Bongkar sebelum muat | Ganti model adalah **muat ulang**: whisper dan suara berhenti, LLM dimuat, mereka kembali (sekitar 30 detik tuli dan bisu) |
| 3 | **Tanpa ROS 2, micro-ROS, atau DDS** | Satu proses berbicara dengan satu mikrokontroler lewat satu kabel adalah masalah permintaan/jawaban, bukan masalah middleware | Tanpa `tf2`, Nav2, atau MoveIt. Lihat [Ditolak](../08-decisions/rejected-alternatives.md) |
| 4 | **Tanpa PyTorch, TensorRT-LLM, NeMo, atau `transformers` di jalur runtime** | Ratusan MB sampai GB tanpa manfaat runtime. `torch` saja 1,5 GB di disk sebelum konteks CUDA. whisper.cpp mencapai bobot yang sama lewat biner sekitar 30 MB | Model harus berupa GGUF atau ONNX. Boleh di lingkungan alat terpisah luring |
| 5 | **Jetson tanpa kepala (headless)**: tanpa desktop, X11, Wayland, atau display manager | Sesi desktop memakan sekitar **600 MB**. "Headless" berarti tanpa lingkungan desktop, **bukan** tanpa layar: panel di robot menggambar langsung ke perangkat keras layar | Layar adalah dashboard browser atau panel Slint |
| 6 | **Tubuh memiliki semua kendali waktu nyata** | Jetson tidak pernah menghasilkan PWM, menjalankan PID, atau menggerakkan motor. Ia mengirim maksud; tubuh mengeksekusi | Semua kinematika dan kendali ada di firmware tubuh |
| 7 | **Watchdog detak tubuh wajib** | Jika Jetson diam lebih dari **500 ms**, tubuh menolkan motor **sendiri**. Jetson suatu saat pasti macet atau dimatikan | Jangan pernah mengandalkan Jetson mengirim `STOP`. Lihat [Watchdog](../03-firmware/watchdog.md) |
| 8 | **Tanpa rahasia di berkas yang dilacak git** | Kredensial masuk `.env`, yang diabaikan git | Berkas konfigurasi tidak memuat kata sandi atau token, begitu pula halaman ini |

> **Batasan 6 dan 7 berkata "STM32".** Tubuh robot adalah ESP32 (dilaporkan pemilik). *Aturannya* berlaku untuk apa pun
> pengendali tubuhnya; *kata*nya sudah usang di 63 berkas yang dilacak. Mengganti nama menyentuh tabel batasan, jadi butuh
> persetujuan dan belum dilakukan. Lihat [Menunggu pemilik proyek](open-decisions.md).

## Aturan yang mengikutinya

### Bahasa dan runtime

* **Sisi Jetson: Python 3.10** dengan `asyncio`, karena JetPack 6.2 membawa 3.10 dan tugasnya mengoordinasi I/O serentak, bukan
  menghitung. Kerja berat sudah ada di mesin asli (llama.cpp, whisper.cpp, ONNX). Tanpa Rust atau C++ di sisi Jetson kecuali
  pemrofilan membuktikan ada loop panas.
* **Satu pengecualian disetujui: `face/`, panel di robot, adalah Rust** (disetujui 2026-09-09). Binding Python Slint berhenti di
  versi alfa untuk platform dan versi Python ini, dan Rust satu-satunya cara menjalankan rilis yang didukung. Ia proses
  terpisah yang tidak mengimpor apa pun dari sisi Python.
* **Sisi tubuh: C** (C++ boleh). Tanpa MicroPython atau runtime tertafsir apa pun: itu merusak determinisme yang menjadi alasan
  adanya mikrokontroler.

### Aturan kerja bagi siapa pun yang mengubah kode (manusia atau AI)

1. **Tanya sebelum menambah ketergantungan** yang tidak ada di `TECH_STACK.md`, dengan biaya RAM-nya dan mengapa yang ada tak
   dapat melakukannya.
2. **Ikuti urutan peta jalan.** Tiap tonggak harus berjalan sebelum yang berikutnya; jangan bangun UI sebelum alur bekerja tanpa kepala.
3. **Setiap komponen jangka panjang adalah layanan yang diawasi** yang bertahan saat mogok.
4. **Ukur, jangan asumsikan.** Catat RAM sebenarnya di `memory-budget.md` di samping perkiraan.
5. **Perbarui dokumen dalam perubahan yang sama**, tambah entri bertanggal di `PROGRESS.md`, dan untuk setiap galat yang
   diperbaiki tambahkan gejala, penyebab, dan perbaikan ke `TROUBLESHOOTING.md` plus uji regresi. Perubahan layanan belum
   selesai sebelum dibaca di bawah **kedua** pengawas.
6. **Manusia menyetujui perubahan arsitektur.** Detail implementasi urusan pelaksana; apa pun yang menyentuh tabel di atas
   butuh persetujuan.

### Definisi selesai untuk sebuah fitur

- [ ] Berjalan di Jetson, tanpa kepala, tanpa sesi desktop
- [ ] Selisih RAM terukur dicatat di `memory-budget.md`
- [ ] Gagal dengan aman: kerusakan tak dapat meninggalkan motor menyala atau robot berbicara selamanya
- [ ] Punya unit systemd jika berjalan lama
- [ ] Terlihat di panel kesehatan dashboard jika layanan
- [ ] Dokumen diperbarui dalam perubahan yang sama

## Dua syarat yang berperilaku seperti batasan

Tidak ada di tabel tetapi diperlakukan keras, karena kegagalan yang digantikannya senyap:

* **Pengenalan dan sintesis suara berjalan di GPU.** `WHISPER_REQUIRE_GPU=1` dan `TTS_REQUIRE_GPU=1`. Alasan terukur: mengurai satu
  ucapan dengan checkpoint `small` 0,50 detik di GPU melawan **23,5 detik** dengan build hanya-CPU, dan suara GPU menjawab dalam median
  626 ms melawan 974 ms. Atur ke 0 hanya dengan sengaja.
* **Data wajah bersifat biometrik dan tetap di robot.** `run/faces.db` dan `run/people.db` diabaikan git, tidak ada yang dikirim ke mana
  pun, dan **tidak ada anti-pemalsuan: jangan pernah menaruh hal penting di baliknya.**
