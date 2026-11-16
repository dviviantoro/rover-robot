# Menunggu pemilik proyek

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/08-decisions/open-decisions.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

Hal-hal yang ditemukan saat menulis buku panduan, atau dicatat "mohon konfirmasi" di pelacak, yang **harus diputuskan
seseorang**. Tidak ada yang diubah tanpa keputusan itu. Angka seperti `#19` adalah **Masalah Diketahui** di
[`PROGRESS.md`](../../../dev-docs/ai/PROGRESS.md) bagian 3.

## Sudah diputuskan pada 2026-11-16

| # | Pertanyaan | Keputusan |
|---|---|---|
| #18 | Ke mana `+vy` dan `+omega`? | `+vy` kiri, `+omega` berlawanan arah jarum jam; enam tempat dikoreksi |
| #20 | `STATUS` tidak pernah diminta | Ditanyakan tiap 2 detik; macet dikonfirmasi lewat dua sampel |
| #21 | Bagaimana watchdog dibuktikan di papan sungguhan? | `watchdog_test.py --port` menjadi pemeriksaan terpandu. **Belum pernah dijalankan di papan** |

## Sebelum ESP32 menjadi tubuh kelas satu

| # | Pertanyaan | Catatan |
|---|---|---|
| **#19** | **Namanya.** 63 berkas dan batasan keras 6 dan 7 menyebut STM32. Ganti menjadi "body controller"? | Menyentuh tabel batasan, jadi butuh persetujuan. Buku panduan memakai "body controller" |
| **#19** | **Chip USB-UART.** `setup_udev.sh` hanya cocok dengan STMicroelectronics, dan `check_board.sh` melihat `ttyACM*` dengan tiga vendor. Papan dengan CP2102 atau CH340 tidak mendapat `/dev/robot-stm32` | Pilihan: perluas keduanya ke vendor dan product id chip; atau andalkan penemuan dengan ping dan nama `/dev/serial/by-id` yang stabil. Yang kedua tanpa perubahan |
| **HAND** | **Servo tidak disebut.** Apa yang dilakukan tubuh pada `HAND wave`? | Menerima dan tidak melakukan apa-apa (`OK HAND`, lalu `DONE HAND`) paling tidak mengejutkan. `ERR HAND unknown` membuat Jetson memberi tahu model bahwa gestur ditolak |
| **Lidar, kamera SPI** | Untuk apa? **Tidak ada kode, protokol, atau unit** untuk keduanya, dan navigasi adalah bukan-tujuan di `PROJECT_BRIEF.md` | Keputusan lingkup. Navigasi juga akan membuka kembali soal ROS 2 |
| **#5** | Penemuan mengirim `{PING_robot}` ke **setiap** `ttyACM*`, `ttyUSB*`, `ttyTHS*`. Lidar pada adaptor USB-UART ikut menerimanya | Daftar vendor yang diizinkan atau opt-in lebih aman. Itu disengaja di `3e37e34` |
| **#6** | Hubungan serial melaporkan **UP padahal papan diam**: `last_rx` dicatat tetapi tak pernah dibaca | "Tak ada baris 2 sampai 3 detik" adalah tanda "tubuh macet" yang murah dan andal |

## Dicatat sebagai "diusulkan, mohon konfirmasi"

| Tanggal | Usulan | Yang perlu disetujui |
|---|---|---|
| 2026-10-22 | `NoNewPrivileges` dicabut pada `robot-core`, `robot-ui`, `robot-tts` | Aturan sudoers (enam unit, tiga kata kerja) adalah batas hak istimewa |
| 2026-10-22 | Dashboard hanya boleh memasukkan dari `run/uploads/` dan `knowledge/` | Tambahkan akar ke `INGEST_ROOTS` jika perlu |
| 2026-10-22 | Penglihatan berjalan di **CPU** | CUDA keempat menghabiskan 0,6 sampai 1,3 GiB |
| 2026-10-22 | Tanpa OpenCV dan tanpa paket baru | Batasan 4 dan lisensi |
| 2026-10-22 | Pengenalan wajah hanya **konteks, bukan keamanan** | Tanpa liveness: foto cocok |
| 2026-10-29 | Baris yang **diketik** dicatat atas nama siapa yang dilihat kamera | Pengetik mungkin bukan orang di depan kamera |

## Yang masih dibutuhkan buku panduan dari Anda

Bab perangkat keras (02) tidak bisa ditulis tanpa pemilik dan robotnya. Dibutuhkan:

| Dibutuhkan | Untuk |
|---|---|
| **Modul ESP32** pastinya, **chip USB-UART**, dan baud yang dipakai firmware | hubungan, aturan udev, halaman kepatuhan |
| **Sumber firmware** (atau di mana letaknya) | bab firmware, yang kini hanya mendokumentasikan acuan |
| **Driver motor**, motor, rasio gigi, **hitungan enkoder per putaran**; diameter roda, jarak sumbu, lebar | konstanta kinematika dan PID |
| **Pohon daya**: jenis dan tegangan baterai, apa yang memberi daya ke Jetson, ESP32, motor, sensor | diagram daya |
| Jenis **layar wajah** dan pemasangannya | apa yang sebenarnya digambar `FACE` |
| **Lidar** (model, cara tersambung) dan **kamera SPI** (model, prosesor yang membacanya) | halaman sensor |
| Apakah ada **servo** | pertanyaan `HAND` |
| **Foto** sasis dan kabel, dan daftar kabel | gambar dan tabel pin |
| Apakah watchdog **pernah diuji di ESP32** | tabel keandalan |
