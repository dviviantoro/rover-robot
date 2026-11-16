# Alternatif yang ditolak dan ditunda

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/08-decisions/rejected-alternatives.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

Tiap baris menyebut apa yang dipertimbangkan, mengapa tidak dipilih, dan **kondisi untuk melihatnya lagi**. Aturan ringkasan
proyek: *jangan memperdebatkan ulang tanpa informasi baru.* Angka berasal dari sumber yang disebut; tanggal pengukurannya ada di
sumber.

## Arsitektur

| Alternatif | Keputusan | Mengapa | Lihat lagi jika |
|---|---|---|---|
| **ROS 2 / micro-ROS** | Ditolak | ROS 2 sepadan untuk pub/sub lintas banyak proses, `tf2`, Nav2, atau `ros2_control`. Di sini ada **satu proses di Jetson berbicara dengan satu mikrokontroler lewat satu kabel**, mengirim perintah sesekali. Itu masalah protokol. Penemuan DDS, siklus hidup node, dan rantai pesan memakan RAM dan waktu belajar tanpa fungsi tambahan | LIDAR atau navigasi kedalaman ditambahkan (Nav2 layak tidak ditulis ulang), lengan multi-sendi butuh kinematika balik (MoveIt2), atau otak dipecah ke beberapa komputer |
| **NanoLLM** (kerangka NVIDIA terpadu) sebagai kerangka utama | Ditolak | Kontainernya menarik PyTorch, TensorRT, `transformers`, MLC, AWQ, DeepSpeed, xformers, dan flash-attention: persis beban yang tak sanggup ditanggung anggaran 8 GB. Backend-nya juga tertinggal dari llama.cpp untuk model baru | Bukan sebagai kerangka. Image `jetson-containers` siap pakai untuk bagian ramping masih dipakai |
| **Rust atau C++ untuk orkestrator Jetson** | Ditolak | Semua kerja berat sudah di mesin asli; orkestrator mengoordinasi I/O (mikrofon, token, potongan audio, pengakuan serial) yang tidak terikat CPU. Binding Rust bagus, jadi ini bukan soal kemampuan melainkan "ketelitian di tempat yang bukan hambatan" | Pemrofilan menunjukkan salah satu fungsi **kita sendiri** terikat CPU: pindahkan fungsi itu saja ke ekstensi. Jangan tulis ulang orkestrator. (`face/` pengecualian Rust yang disetujui, dengan alasan lain) |
| **JetPack 7.2** | Ditunda | Mendukung Orin Nano dan lebih hemat memori, tetapi ekosistem pihak ketiga belum menyusul, dan butuh pem-flash-an ISO dan firmware JetPack 6.x dulu | Alur sudah stabil dan wheel serta kontainer yang dibutuhkan punya build JetPack 7 |
| **Compositor** (`cage`, `weston`) untuk panel | Ditolak | Terukur: pengguna layanan dapat mengambil **dan melepas** DRM master tanpa root dan tanpa manajer seat, jadi compositor menambah proses dan 40 sampai 80 MiB untuk apa-apa. Panel terukur **11,0 MiB** resident, dibanding 60 MiB untuk dashboard | Tumpukan layar berhenti bekerja sama. `nvidia-l4t-weston` cadangannya, sudah terpasang |
| **Pustaka grafik** untuk dashboard | Ditolak | Ratusan kilobita urai pada papan dengan sekitar 100 MiB sisa, hanya untuk menggambar dua garis dan satu batang per giliran. Grafik adalah SVG tulis tangan | Grafik menjadi sesuatu yang tak bisa dilakukan yang tulis tangan |
| **MicroPython** pada tubuh | Ditolak | Runtime tertafsir merusak determinisme yang menjadi alasan memiliki mikrokontroler | |
| **Protokol serial biner** | Ditolak | Lajunya rendah, dan dapat membaca lalu lintas dengan `screen` lebih berharga daripada byte yang dihemat | Laju naik mendekati loop kendali. Maka tubuh sebaiknya memiliki lebih banyak, bukan protokol yang tumbuh |

## Suara dan bahasa

| Alternatif | Keputusan | Mengapa | Lihat lagi jika |
|---|---|---|---|
| **Qwen3-ASR** sebagai mesin suara | **Dihapus 2026-09-14** (manusia) | Ia transkriber lebih baik, tetapi **tidak mengeluarkan tag bahasa sama sekali**, dan robot ini merutekan tiga bahasa dari tag itu. Mempertahankan dua mesin berarti membangun dan mengukur setiap fitur dua kali untuk yang tak dapat mengerjakan tugasnya | |
| **Nemotron ASR dengan MagpieTTS** | Ditolak untuk versi 1 | Bahasa Arab dan Inggris sangat baik, tetapi **keduanya tidak mendukung Bahasa Indonesia** | Persyaratan Indonesia dicabut, atau sebagai suara Inggris dan Arab opsional |
| **Whisper lewat PyTorch** (`transformers`) | Ditolak | Batasan 4. Bobot yang sama berjalan lewat whisper.cpp sebagai biner sekitar 30 MB | |
| **Memilih model lebih kecil ketimbang terkuantisasi** | Ditolak | Terukur pada sembilan sampel di tiga bahasa: turbo **q5_0 melawan fp16 lebih kecil 656 MiB, lebih cepat 223 ms, dan transkripnya sama persis karakter demi karakter**. Presisi sumbu yang murah; jumlah parameter yang mahal | |
| **Peredam derau menyala bawaan** (GTCRN) | Mati bawaan | Terhadap whisper ia membuat transkrip **lebih buruk di setiap SNR yang dicoba**: +27% sampai +314% galat kata pada derau stasioner, +58% sampai +215% pada riuh. Whisper dilatih pada audio berisik dan artefak peredam adalah kerusakan yang belum pernah dilihatnya. Ujinya sintetis, jadi ia hadir satu pengaturan jauhnya | `./scripts/measure/denoise_ab.py --noise <30 detik ruang nyata>` menyatakan ia membantu di sana |
| **all-MiniLM-L6-v2** untuk embedding | Ditolak | Berpusat pada Inggris: diam-diam menurunkan pencarian Arab dan Indonesia. Qwen3-Embedding-0.6B menambah sekitar 160 MB dan terverifikasi **8 dari 8 pencarian benar, 5 dari 5 lintas bahasa** | |
| **`k2-fsa/OmniVoice`** untuk suara | Ditolak | Model bahasa audio PyTorch: memuatnya butuh `torch` dan `transformers` di jalur runtime (batasan 4). Tak ada ekspor ONNX. Kloning suaranya berguna di lingkungan alat terpisah, untuk *menghasilkan* suara yang kemudian diucapkan mesin ramping | Ekspor ONNX muncul |
| **Suara CPU, untuk menghemat memori** | Tidak dipilih | Suara GPU menghemat **348 ms di setiap giliran** dan tidak menurun di bawah beban. Tetapi rapuh: dengan sebagian memorinya di swap ia butuh 10,5 detik. Suara CPU lebih baik hanya saat ruang kosong hilang | Ruang kosong memori hilang: atur `TTS_USE_GPU=0` |
| **Menggabung kalimat antre** dalam satu panggilan suara | Mati (`TTS_BATCH_SENTENCES=1`) | Digabung, model menyisipkan jedanya sendiri (15,0 karakter/detik); terpisah tidak (18,0), dan mana yang terjadi bergantung pada waktu. Kata yang sama keluar berbeda 12 sampai 19%. Ukuran batch 1 membuatnya 0,1% | |

## Penglihatan dan orang

| Alternatif | Keputusan | Mengapa | Lihat lagi jika |
|---|---|---|---|
| **YOLOv8** (Ultralytics) | Ditolak | **AGPL-3.0**, dan paketnya mengimpor PyTorch. YOLOX-Tiny (Apache-2.0) dipakai: 20 MiB, 145 ms | |
| **InsightFace** | Ditolak | Model pralatihnya **hanya riset nonkomersial**. SFace (Apache-2.0) dan YuNet (MIT) dipakai | |
| **OpenCV, Pillow** di runtime | Ditolak | Paket baru untuk sekitar 100 baris numpy (`robot/vision/imaging.py`) | |
| **Penglihatan di GPU** | Tidak dipilih | CUDA keempat menghabiskan 0,6 sampai 1,3 GiB. Di CPU layanan sekitar **213 MiB** dan sekitar 0,27 inti saat ruangan diam | `VISION_USE_GPU=1` ada dan belum diukur |
| **Model bahasa-visual** | Di luar lingkup | Ringkasan proyek mencantumkannya sebagai bukan-tujuan; pendeteksi kecil menjawab "siapa dan apa di depan saya" | Muncul alasan membaca gambar sembarang |
| **Menganggap pengenalan wajah sebagai keamanan** | Ditolak | Tanpa pemeriksaan keaktifan: foto orang terdaftar dikenali sebagai orang itu | Jangan pernah menaruh hal penting di baliknya |

## Aturan di balik baris-baris ini

Lisensi ikut memilih sebagian model ini, bukan hanya akurasi: **mengubah model berarti membaca ulang lisensinya**, bukan hanya
skornya. Dan "ditolak" di proyek ini hampir selalu disertai angka. Baris tanpa angka adalah celah catatan, bukan alasan.

Sumber: [`PROJECT_BRIEF.md`](../../../planning/PROJECT_BRIEF.md) bagian Rejected alternatives,
[`TECH_STACK.md`](../../../planning/TECH_STACK.md), [`AGENTS.md`](../../../AGENTS.md),
[`face.md`](../../../dev-docs/modules/face.md), [`vision.md`](../../../dev-docs/modules/vision.md).
