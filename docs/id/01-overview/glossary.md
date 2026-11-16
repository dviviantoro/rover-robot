# Daftar istilah

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/01-overview/glossary.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama. Istilah teknis tetap dalam bahasa Inggris di dalam tanda kurung.

Istilah menurut urutan Anda akan bertemu dengannya, dikelompokkan menurut tempatnya.

## Robot

### Pengendali tubuh (body controller)
Papan kecil yang memiliki semua yang waktu nyata: kecepatan roda, pewaktuan, layar wajah, dan penghentian keselamatan. Ia menerima
perintah teks pendek dari Jetson lewat kabel serial USB.

**Di robot ini ia ESP32** (dilaporkan pemilik pada 2026-11-12). Di repositori ini ia disebut **STM32** hampir di mana-mana
(`firmware/stm32`, `tools/stm32_sim`, pengaturan `STM32_PORT`, `/dev/robot-stm32`, dan batasan keras 6 dan 7 di
[`AGENTS.md`](../../../AGENTS.md)), karena proyek dimulai dengan rancangan STM32. Firmware acuan di `firmware/stm32` tetap
deskripsi protokol yang bisa dijalankan, yang dipakai kedua papan. Mengganti nama di mana-mana menyentuh batasan keras, jadi itu
perubahan terpisah yang butuh persetujuan pemilik.

### Otak (brain)
Jetson Orin Nano 8 GB dan sembilan program di atasnya.

### Watchdog (detak)
Jetson mengirim `PING` tiap 200 ms. Jika tubuh tak mendengar apa-apa selama 500 ms, ia menghentikan motor sendiri. Keselamatan robot
tak pernah bergantung pada Jetson yang masih hidup. Teruji terhadap firmware acuan dan simulator 🧪; belum diperiksa di ESP32 📐.

### Roda mecanum
Roda dengan rol bersudut. Dengan empat roda, robot dapat maju, ke samping, dan berbelok, dalam kombinasi apa pun.

### Enkoder (encoder)
Sensor pada motor yang menghitung berapa jauh ia berputar, sehingga tubuh dapat mengendalikan kecepatan, bukan hanya daya.

### PID
Loop kendali yang membuat roda menahan kecepatan yang diminta, memakai enkoder.

## Mendengar

### VAD (deteksi aktivitas suara)
Model kecil (Silero) yang berkata "seseorang sedang bicara" dan "mereka berhenti". Ia bukan pengurai ucapan.

### ASR (pengenalan ucapan otomatis)
Mengubah audio menjadi teks. Di sini: whisper.cpp, di `robot-whisper`.

### Ucapan (utterance)
Satu rentang bicara dari pendeteksi suara terbuka sampai tertutup.

### Penentuan akhir (endpointing)
Memutuskan bahwa Anda sudah selesai. Biayanya sepanjang jeda yang ditunggunya (`vad_min_silence`), di setiap giliran.

### Gerbang (gate)
Pemeriksaan yang membuang ucapan sebelum menjadi giliran: terlalu pelan, keyakinan pengurai rendah, atau bahasa yang tak didengarkan robot.

### Menyela (barge-in)
Memotong robot saat ia berbicara.

### Kata bangun (wake word)
Kata (`robot`) yang harus muncul di transkrip sebelum giliran dimulai, pada mode `wake_word`.

## Berpikir

### LLM (model bahasa besar)
Model yang menulis jawaban. Di sini yang kecil dalam format GGUF, dijalankan llama.cpp.

### GGUF
Format berkas yang dimuat llama.cpp untuk sebuah model.

### Kuantisasi (q4, q5, q8)
Menyimpan angka model dengan bit lebih sedikit agar memakai memori lebih kecil. Di papan ini model terkuantisasi pada kelas ukuran yang
sama adalah penghematan murah: presisi lebih murah daripada jumlah parameter.

### Jendela konteks (`LLM_CTX`)
Berapa banyak teks yang dapat dilihat model sekaligus: prompt, riwayat, dan balasannya sendiri bersama-sama.

### Prompt
Semua yang dikirim ke model untuk satu giliran: persona, catatan dari dokumen, instruksi bahasa, dengan siapa ia bicara, percakapan
terakhir, dan apa yang dilihat kamera.

### Persona
Instruksi tetap (nama, sikap, aturan bicara) yang dikirim bersama setiap giliran. Ia ada di halaman Knowledge dashboard.

### Tata bahasa (keluaran terbatas)
llama.cpp dapat memaksa model hanya menghasilkan teks yang cocok dengan skema. Robot memakainya agar balasan selalu objek JSON yang sama
dan tak pernah kembali sebagai kalimat setengah jadi.

### JSON balasan (reply JSON)
Objek yang dikembalikan model: `answer`, `language`, `foot_direction`, `foot_step`, `hand_movement`, `face`, `style`. Hanya `answer`
yang diucapkan. Lihat [JSON balasan](../05-interfaces/reply-json.md).

### Pengisi (acknowledgement)
"Baik, sebentar" singkat yang diucapkan selagi jawaban disiapkan. Dirender di muka, jadi mulai dalam sekitar 123 ms.

### RAG (pembangkitan dengan pencarian)
Mencari catatan di dokumen Anda dan memberikannya ke model bersama pertanyaan. Lihat [Pengetahuan](../04-brain/knowledge.md).

### Embedding
Daftar angka yang menangkap makna sepotong teks, sehingga "makna serupa" dapat ditemukan bahkan lintas bahasa.

### Potongan (chunk)
Bagian dokumen, dipecah pada judulnya bila mungkin, disimpan dan dicari sendiri-sendiri.

## Berbicara

### TTS (teks ke suara)
Mengubah teks menjadi suara. Di sini: Supertonic, satu suara untuk tiga bahasa, di `robot-tts`.

### Pemotong (chunker)
Memotong jawaban yang mengalir menjadi klausa agar yang pertama dapat diucapkan lebih awal.

### Kata pertama (time to first word)
Jeda dari akhir ucapan Anda ke bunyi pertama robot. Angka yang menjadi sasaran penyetelan seluruh alur.

## Papan

### Memori terpadu (unified memory)
Di Jetson, CPU dan GPU mengambil dari satu kolam RAM. Memuat model di GPU memakai memori yang sama dengan yang lain.

### MiB
Mebibita, 1024 × 1024 byte. Kolamnya **7620 MiB**, bukan 8192: sisanya dicadangkan perangkat keras.

### Swap
Memori yang didorong ke disk saat RAM habis. Halaman yang di-swap harus dibaca kembali sebelum dipakai. Terukur di sini, ia mengubah
kata pertama 0,6 sampai 0,9 detik menjadi 10,5 detik.

### OOM (kehabisan memori)
Keadaan saat sistem membunuh proses agar bertahan. Sasaran rancangan: tak pernah melihatnya.

### Konteks CUDA
Biaya memori tetap memakai GPU dari sebuah proses, dibayar sekali per proses sebelum model apa pun dimuat.

### Wajib GPU (GPU-required)
STT dan TTS harus berjalan di GPU (`WHISPER_REQUIRE_GPU`, `TTS_REQUIRE_GPU`). Jika GPU tak bisa didapat, keduanya melaporkan galat
alih-alih diam-diam berjalan lambat di CPU.

## Menjalankannya

### Layanan (service)
Satu dari sembilan program jangka panjang. Masing-masing dinyalakan ulang jika mati.

### Pengawas (supervisor)
Yang menyalakan dan menyalakan ulang layanan. Saat pengembangan: `./run.sh` (lewat `scripts/run_stack.sh`). Saat produksi: unit
`systemd` di `deploy/systemd/`.

### Keepalive
Bagian `robot-core` yang menyadari mesin mati (LLM, pengurai, layanan mikrofon, suara, kamera) dan menyalakannya ulang. Lihat
[Keandalan](../04-brain/reliability.md).

### Dashboard
Halaman web di port 8081 untuk berbicara dengan robot, membaca lognya, dan mengubah pengaturannya.

### Panel
Layar opsional yang terpasang pada robot, digambar oleh `robot-face`.

### Pin (pin dashboard)
Nilai yang dipilih di dashboard. Ia mengalahkan nilai di `config/*.env`, dan pin bertahan sampai dihapus. `./run.sh <mode> --check`
mendaftarnya.
