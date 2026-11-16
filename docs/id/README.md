# Buku panduan robot

> **Versi Bahasa Indonesia.** Diterjemahkan dari versi Inggris ([`../en/README.md`](../en/README.md))
> pada **2026-11-12**, dan peta bab serta tabel "Baca ini jika" diperbarui pada **2026-11-16**.
> Versi Inggris adalah naskah utama: jika keduanya berbeda, ikuti
> versi Inggris. Halaman lain masih dalam bahasa Inggris dan akan diterjemahkan bertahap.

Robot yang bisa diajak bicara dalam **Bahasa Inggris, Bahasa Indonesia, atau Bahasa Arab**.
Ia mendengarkan, berpikir, menjawab dengan suara, melihat siapa yang ada di depannya, dan
bergerak. Semuanya berjalan **di dalam robot itu sendiri, tanpa cloud**: sebuah Jetson Orin
Nano sebagai otak, dan sebuah mikrokontroler sebagai tubuh.

![Konteks sistem: seorang pengguna, otak Jetson dengan sembilan layanan, tubuh ESP32, dan browser](../assets/diagrams/system-context.svg)

*Cara membaca gambar: pengguna berbicara kepada robot lewat mikrofon dan speaker, dan
terlihat oleh kamera. Di dalam robot, Jetson menjalankan sembilan program kecil
("layanan"). Salah satunya, `robot-core`, mengirim perintah teks biasa lewat kabel USB ke
mikrokontroler, yang mengubahnya menjadi gerakan. Browser di jaringan yang sama dapat
membuka dashboard.*

*(Label di dalam gambar memakai istilah teknis dalam bahasa Inggris, seperti nama layanan
dan perintah. Nama berkas, perintah, dan pengaturan memang tidak diterjemahkan.)*

## Seberapa yakin kita? Empat lencana

Tubuh robot belum pernah dijalankan bersama Jetson ini, jadi halaman-halaman ini
menyebutkan sejauh mana setiap klaim sudah terbukti:

| Lencana | Artinya |
|---|---|
| ✅ **Terukur** | Sudah dijalankan di robot ini dan diperiksa: ada angka atau baris log |
| 🧪 **Diuji tanpa perangkat keras** | Hanya dicakup oleh unit test atau simulator di komputer |
| 📐 **Dirancang atau dilaporkan** | Ada di rancangan, atau disampaikan pemilik proyek, tetapi belum pernah diperiksa di sini |
| ❓ **Belum diketahui** | Butuh keputusan pemilik, atau perlu dilihat langsung pada robotnya |

## Baca ini jika Anda…

| Anda… | Baca |
|---|---|
| Baru, dan ingin tahu ini apa | [Apa itu robot ini](../en/01-overview/what-it-is.md) *(Inggris)* |
| Penasaran bagaimana percakapan berjalan | [Satu giliran, dari awal sampai akhir](../en/01-overview/one-turn.md) *(Inggris)* |
| Mencari arti sebuah istilah | [Daftar istilah](../en/01-overview/glossary.md) *(Inggris)* |
| Ingin tahu program mana mengerjakan apa | [Otak: proses dan port](../en/04-brain/README.md) *(Inggris)* |
| Menelusuri robot yang lambat atau "tuli" | [Alur suara](../en/04-brain/voice-pipeline.md), lalu [Keandalan](../en/04-brain/reliability.md) *(Inggris)* |
| Memilih atau mengganti model | [Model dan memori](../en/04-brain/models-and-memory.md) *(Inggris)* |
| Mengajari robot dari dokumen | [Pengetahuan](../en/04-brain/knowledge.md) *(Inggris)* |
| Menulis atau memeriksa firmware tubuh robot | [Tubuh: firmware](../en/03-firmware/README.md), lalu [ESP32 sebagai tubuh](../en/03-firmware/esp32-conformance.md) *(Inggris)* |
| Memastikan robot berhenti saat otaknya mati | [Watchdog](../en/03-firmware/watchdog.md) *(Inggris)* |
| Memeriksa arah putar roda | [Gerak: dari kalimat ke empat roda](../en/03-firmware/motion.md) *(Inggris)* |
| Memasang kabel tubuh robot | Bab perangkat keras masih direncanakan dan butuh pemilik proyek. Untuk sekarang: [`PROJECT_BRIEF.md`](../../planning/PROJECT_BRIEF.md) bagian Hardware dan [`robot_pins.h`](../../firmware/stm32/include/robot_pins.h), usulan yang belum pernah diperiksa pada papan sungguhan |
| Menyambungkan layar, skrip, atau tubuh baru | [Antarmuka](../en/05-interfaces/README.md) *(Inggris)* |
| Mengubah pengaturan, tetapi tidak terjadi apa-apa | [Konfigurasi](../en/05-interfaces/configuration.md) *(Inggris)* |
| Memasang papan atau menyalakan robot | [Pemasangan](07-operations/install.md), lalu [Menjalankan sehari-hari](07-operations/running.md) |
| Memakai dashboard atau layar di robot | [Dashboard dan panel](../en/06-dashboard/README.md) *(Inggris)* |
| Mengetahui apa yang diketahui tentang perangkat keras | [Perangkat keras](../en/02-hardware/README.md) *(Inggris)* |
| Robot "tuli", bisu, lambat, atau macet | [Indeks gejala](../en/07-operations/troubleshooting-index.md) *(Inggris)* |
| Bertanya "mengapa bukan ROS 2?" atau "mengapa model itu?" | [Keputusan](../en/08-decisions/README.md) *(Inggris)* |
| Mencari yang menunggu keputusan pemilik | [Menunggu pemilik proyek](../en/08-decisions/open-decisions.md) *(Inggris)* |
| Akan mengubah kode (manusia atau AI) | [`AGENTS.md`](../../AGENTS.md) dulu |
| Menemui pesan galat | [`TROUBLESHOOTING.md`](../../dev-docs/ai/TROUBLESHOOTING.md), bisa dicari berdasarkan gejala |

## Peta buku panduan

| Bab | Keadaan |
|---|---|
| **01 Gambaran umum**: apa itu robot ini, satu giliran, daftar istilah | ✍️ ditulis (Inggris) |
| **02 Perangkat keras**: apa yang diketahui dan belum, sisi Jetson, kabel acuan | ◐ sebagian (Inggris): komponen, daya, dan kabel tubuh yang sebenarnya butuh pemilik proyek |
| **03 Firmware**: tubuh acuan, watchdog, gerak dan tanda roda mecanum, simulator, apa yang harus dilakukan ESP32 | ✍️ ditulis (Inggris) |
| **04 Otak**: proses, alur suara, model dan memori, pengetahuan, penglihatan dan orang, keandalan | ✍️ ditulis (Inggris) |
| **05 Antarmuka**: protokol serial, JSON balasan, WebSocket, referensi konfigurasi | ✍️ ditulis (Inggris) |
| **06 Dashboard dan panel**: setiap halaman, dan panel di robot | ✍️ ditulis dari kode sumber (Inggris), belum ada tangkapan layar |
| **07 Operasional**: pemasangan, mode menjalankan, systemd, indeks gejala | ✍️ ditulis. **Sudah diterjemahkan**: [ikhtisar](07-operations/README.md), [pemasangan](07-operations/install.md), [menjalankan](07-operations/running.md). Sisanya Inggris |
| **08 Keputusan**: batasan keras, alternatif yang ditolak, yang menunggu pemilik proyek | ✍️ ditulis (Inggris) |

Halaman depan dan tiga halaman operasional sudah diterjemahkan; sisanya menyusul bertahap.

## Fakta singkat (per 2026-11-12)

| | |
|---|---|
| Otak | Jetson Orin Nano 8 GB (Super), JetPack 6.2.1, tanpa desktop. **7620 MiB** RAM dapat dipakai, dibagi bersama oleh CPU dan GPU |
| Tubuh | Mikrokontroler lewat serial USB: sebuah **ESP32** (dilaporkan pemilik proyek; lihat [daftar istilah](../en/01-overview/glossary.md#body-controller)) |
| Bahasa | Inggris, Indonesia, Arab; dipilih per kalimat dari kata-kata yang diucapkan |
| Jaringan | Tidak ada yang keluar dari robot. Dashboard hanya ada di jaringan Anda sendiri |
| Kecepatan ✅ | Dari akhir ucapan sampai kata pertama jawaban: **median 2181 ms, terbaik 1554 ms** (target 1500 ms). Kalimat singkat "oke, sebentar ya" bisa mulai dalam sekitar **123 ms** |
| Pengujian | 1156 tes Python dan 634 pemeriksaan inti firmware, semuanya lulus pada 2026-11-11 |
| Proses | 9: `robot-llm`, `robot-whisper`, `robot-asr`, `robot-tts`, `robot-core`, `robot-embed`, `robot-vision`, `robot-ui`, `robot-face` |

Angka berasal dari [`PROGRESS.md`](../../dev-docs/ai/PROGRESS.md) dan
[`AGENTS.md`](../../AGENTS.md). Keduanya adalah catatan yang selalu diperbarui; tabel ini hanya
potret pada satu tanggal.

## Aturan penulisan

* **Versi Inggris adalah naskah utama.** Halaman Indonesia mengikutinya dan menyebut tanggal
  penerjemahannya.
* **Halaman menyebutkan sumbernya.** Sebuah angka diikuti keterangan di mana ia diukur.
  Klaim tanpa lencana dan tanpa sumber adalah kesalahan pada halaman itu.
* **Gambar berupa teks.** Diagram SVG bisa disunting dengan tangan; diagram alur memakai Mermaid.
* **Nama berkas, perintah, dan pengaturan tidak diterjemahkan.**
