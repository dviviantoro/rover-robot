# Apa itu robot ini

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/01-overview/what-it-is.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama: jika berbeda, ikuti versi Inggris.

Anda mengajaknya bicara. Ia mendengarkan, berpikir, menjawab dengan suara dan, jika diminta, bergerak.
Ia berbicara dalam **Bahasa Inggris, Bahasa Indonesia, dan Bahasa Arab**, menentukan bahasa mana yang Anda
pakai dari kata-kata Anda sendiri, dan menjawab dalam bahasa itu, tanpa pengaturan yang perlu diubah.
Semuanya berjalan di dalam robot: tidak ada layanan cloud di mana pun dalam alurnya.

## Yang bisa dilakukan sekarang

| Kemampuan | Dalam satu baris | Keadaan |
|---|---|---|
| **Mendengar** | Mikrofon selalu aktif, pendeteksi suara, lalu pengurai ucapan terbuka whisper.cpp di GPU | ✅ |
| **Berpikir** | Model bahasa yang berjalan di Jetson, dibatasi agar menjawab dalam satu bentuk JSON tetap | ✅ |
| **Berbicara** | Satu suara sintetis untuk ketiga bahasa. Ia mulai bicara **sebelum jawaban selesai** | ✅ |
| **Dipotong** | Dapat diatur berhenti bicara saat Anda menyela (mode `interrupt`). Bawaannya `no_interrupt`, karena speaker dan mikrofon adalah satu perangkat USB dan robot akan mendengar dirinya sendiri | ✅ |
| **Belajar dari dokumen** | Masukkan PDF atau ketik sebuah fakta. Dapat dicari dalam salah satu dari tiga bahasa | ✅ |
| **Melihat** | Kamera menemukan objek dan wajah. Deskripsi satu kalimat tentang pemandangan diberikan ke model | ✅ (belum dicoba dengan orang sungguhan) |
| **Tahu siapa yang bicara** | Menanyakan nama orang asing sekali, lalu mengenalinya, menyapanya, dan melanjutkan percakapan | ✅ (belum dicoba dengan orang sungguhan) |
| **Bergerak** | Jawaban model membawa "jalan maju 2 langkah" atau "lambaikan tangan"; robot mengubahnya menjadi perintah serial singkat dan terbatas | 🧪 (tubuh belum terpasang; lihat di bawah) |

Sumber: ringkasan status [`PROGRESS.md`](../../../dev-docs/ai/PROGRESS.md),
[`PROJECT_BRIEF.md`](../../../planning/PROJECT_BRIEF.md).

## Yang sengaja tidak dilakukan

* **Tanpa cloud, selamanya.** Tidak ada cadangan ke layanan daring. Ini aturan keras.
* **Tanpa navigasi.** Tidak ada pemetaan, penghindaran rintangan, atau perencanaan jalur. (Lidar 360 derajat
  dilaporkan ada di tubuh, tetapi tidak ada yang di repositori ini membacanya.)
* **Tanpa model bahasa-visual.** Kamera memberi makan pendeteksi kecil, bukan model yang membaca gambar.
* **Pengenalan wajah bukan keamanan.** Ia ada untuk menyapa orang dan melanjutkan percakapan. Tidak ada
  pemeriksaan bahwa wajah itu orang hidup, jadi tidak boleh ada hal penting di baliknya.
* **Tanpa ROS 2.** Satu program berbicara dengan satu mikrokontroler lewat satu kabel. Lihat
  [`PROJECT_BRIEF.md`](../../../planning/PROJECT_BRIEF.md) bagian Rejected alternatives.

## Perangkat keras sekilas

Gambarnya ada di [halaman depan](../README.md). Tabel ini menunjukkan seberapa jauh tiap bagian terbukti.

| Bagian | Fungsi | Keadaan | Bukti |
|---|---|---|---|
| Jetson Orin Nano 8 GB (Super) | Semua AI dan orkestrasi | ✅ | JetPack 6.2.1, mode daya MAXN_SUPER, tanpa desktop |
| Kartu suara USB, mikrofon | Masukan audio | ✅ | Transkripsi diperiksa dalam ketiga bahasa |
| Kartu suara USB, speaker | Keluaran audio | ✅ | Pemutaran waktu nyata diperiksa; speaker dikunci menurut nama di `config/tts.env` |
| Kamera CSI, IMX219 | Penglihatan | ✅ | Ditemukan dan diuji pada 3 bingkai per detik, berhenti bersih |
| Layar HDMI/DisplayPort (opsional) | Panel di robot | ✅ | Jalur layar bekerja pada 1366×768; tanpa sentuh atau papan ketik |
| **Pengendali tubuh ESP32** | Kendali waktu nyata dan keselamatan | 📐 | Dilaporkan pemilik. Firmware-nya tidak ada di repo dan hari ini tidak terpasang ke Jetson ini |
| 4 motor DC dengan enkoder, roda mecanum | Bergerak ke segala arah | 📐 | Ada dalam rancangan ([`robot_pins.h`](../../../firmware/stm32/include/robot_pins.h)); arah roda belum pernah diperiksa |
| Layar wajah | Ekspresi | 📐 | Dilaporkan pemilik; perintah `FACE` didefinisikan dan disimulasikan |
| Tegangan baterai dan sensor lain | Baris status `STAT` | 📐 | Didefinisikan di protokol; firmware acuan membaca pembagi tegangan baterai |
| Servo untuk gestur tangan | Perintah `HAND` | ❓ | Protokol punya `HAND`; pemilik tidak menyebut servo |
| Lidar 360 derajat, kamera SPI | Belum dipakai apa pun | ❓ | Dilaporkan pemilik; tidak ada kode atau protokol untuknya di sini |

**Arti "tubuh" di repositori ini.** Proyek dimulai dengan rancangan STM32, jadi firmware acuan dan simulator
host bernama `firmware/stm32` dan `tools/stm32_sim`. Pengendali robot yang sebenarnya adalah ESP32 yang berbicara
dengan protokol teks yang sama. Firmware acuan tetap menjadi deskripsi protokol yang bisa dijalankan, dan itulah
yang dijalankan uji. Lihat [daftar istilah](../../en/01-overview/glossary.md#body-controller) *(Inggris)*.

## Empat aturan yang menjelaskan sebagian besar keputusan

1. **Satu kolam 8 GB.** CPU dan GPU berbagi, jadi melampauinya berarti proses dimatikan, bukan melambat. Setiap
   model dihitung biayanya terhadap kolam sebelum dimuat. Lihat [Model dan memori](../../en/04-brain/models-and-memory.md) *(Inggris)*.
2. **Tubuh memiliki semua yang waktu nyata.** Jetson berkata *apa* ("maju sedikit"); mikrokontroler memutuskan
   *bagaimana* (kecepatan roda, waktu). Jika Jetson diam setengah detik, tubuh menghentikan motor sendiri.
3. **Semuanya layanan yang diawasi.** Sembilan program kecil, masing-masing dinyalakan ulang jika mati, sehingga
   satu kerusakan tidak menjatuhkan robot. Lihat [Otak](../../en/04-brain/README.md) *(Inggris)*.
4. **Layar bersifat opsional.** Dashboard dan panel di robot hanyalah tamu: menutup atau merusakkannya tidak
   mengubah apa pun pada robot.

Daftar lengkap beserta alasannya ada di [`AGENTS.md`](../../../AGENTS.md) bagian Hard constraints.

## Berikutnya

* [Satu giliran, dari awal sampai akhir](../../en/01-overview/one-turn.md) *(Inggris)*: apa yang terjadi antara "halo" dan kata pertama.
* [Otak](../../en/04-brain/README.md) *(Inggris)*: sembilan program dan cara mereka berkomunikasi.
