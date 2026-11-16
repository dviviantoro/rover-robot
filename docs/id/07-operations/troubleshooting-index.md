# Ada yang salah: indeks gejala

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/07-operations/troubleshooting-index.md) pada
> **2026-11-16**. Kolom "Cari" tetap **Bahasa Inggris**, karena itu awal judul entri di `TROUBLESHOOTING.md` yang berbahasa Inggris.

[`TROUBLESHOOTING.md`](../../../dev-docs/ai/TROUBLESHOOTING.md) adalah log galat: sekitar 70 entri, masing-masing **gejala,
penyebab, perbaikan**, ditulis saat masalahnya nyata. Panjang, jadi halaman ini jalan masuknya. Temukan gejala Anda di sini, lalu
**cari `TROUBLESHOOTING.md` dengan kata dalam tanda kutip**.

## Pertama, tiga hal untuk dilihat apa pun gejalanya

1. **`./run.sh status`**, dan halaman **Services**: apakah semuanya berjalan, dan di GPU atau CPU? Lampu hijau hanya berarti
   layanan menjawab, bukan berfungsi: beberapa entri di bawah tentang semua lampu hijau.
2. **`./scripts/measure/whats_running.py`**: lihat kolom `swap` dulu. Swap mengubah suara 0,6 detik menjadi 10 detik.
3. **`.cache/tmp/core.log`** (atau `journalctl -u robot-core`). Hampir setiap kegagalan meninggalkan tepat satu baris di sana.

## Mendengar

| Anda melihat | Cari |
|---|---|
| Tak mendengar apa-apa dan tak ada galat | "The robot hears nothing, and nothing reports an error" |
| Tuli selamanya dan tak mencatat apa-apa | "The robot goes permanently deaf, and nothing logs anything" |
| Mengganti mikrofon atau speaker USB | "You replaced the USB mic/speaker and the robot went deaf" |
| Memilih ukuran whisper lain dan jadi tuli | "Choosing a whisper model in the dashboard leaves the robot deaf" |
| whisper mati saat mulai | "whisper-server dies at startup with a ggml backtrace" |
| whisper bilang memakai GPU tetapi lambat | "whisper says `use gpu = 1` and decodes at CPU speed" |
| Menjawab sesuatu yang tak diucapkan siapa pun, sering dalam bahasa Mandarin | "The robot answers something nobody said", "a cough that whisper called Chinese" |
| Bagus di ruang sepi, kacau di ruang ramai | "Transcription is fine in a quiet room", lalu "Noise suppression made it worse" |
| Menyela dirinya atau menjawab suaranya sendiri | "The robot interrupts itself", "still answers its own voice in no_interrupt mode" |
| Berhenti mendengar setelah beralih ke `interrupt` | "Switched to "interrupt" mid-reply" |
| Giliran berakhir seketika setelah dimulai | "A turn ends the instant it starts, after a barge-in" |

## Berbicara

| Anda melihat | Cari |
|---|---|
| Mendengar tetapi tak pernah bicara | "The robot listens but never speaks" |
| Berhenti bicara dan halaman Services merah | "The robot stopped speaking, and the Services page is red" |
| `aplay: audio open error` | "Device or resource busy" |
| Suara bilang di GPU tetapi lambat | "Supertonic says it is on the GPU and synthesises at CPU speed" |
| `CUDNN_STATUS_INTERNAL_ERROR` | "CUDNN_STATUS_INTERNAL_ERROR from robot-tts" |
| Jeda setelah setiap kalimat | "The robot pauses after every sentence" |
| Suara lambat atau cepat, berbeda tiap kali | "The voice plays slow, or fast, and not the same way twice" |
| Suara tidak ditemukan | "The Supertonic voice cannot be found on either page" |
| "Baik, sebentar" terlambat atau kosong | "The acknowledgement is slow", "The acknowledgement cache empties itself" |
| `first_audio_ms` tampak lebih baik daripada suaranya | "`first_audio_ms` looks better than the robot sounds" |

## Berpikir dan yang dikatakannya

| Anda melihat | Cari |
|---|---|
| Berkata "Give me a moment" untuk semuanya | "The robot says "Give me a moment" to everything", "Still "Give me a moment"" |
| Jawaban berhenti di tengah kalimat | "Answers stop in the middle of a sentence" |
| Giliran tak pernah selesai dan robot diam | "A turn never finishes, and the robot goes quiet" |
| `request (N tokens) exceeds the available context size` | "exceeds the available context size" |
| Tumpukan bilang LLM sehat tetapi tak ada LLM | "The stack prints "llama-server healthy" and comes up with no LLM" |
| Model berhenti menghasilkan penanda aksinya | "The model stops emitting [lang:xx] tags" |
| Pertanyaan Inggris dijawab Indonesia (atau sebaliknya) | "I spoke English and it answered in Indonesian", "answers in English" |
| Persona panjang mengubah robot | "A long prompt silently changes the persona" |

## Lambat, atau kehabisan memori

| Anda melihat | Cari |
|---|---|
| Jawaban lambat dan menyetel tak membantu | "Voice replies feel slow, and tuning the dashboard does not help" |
| Jadi lambat dan semua pengukur sehat | "The robot got slow, and every gauge says it is healthy" |
| `cudaMalloc failed: out of memory` padahal GB tersedia | "`cudaMalloc failed: out of memory` while `free` shows GB available" |
| LLM tak dapat menyala ulang setelah mogok | "A crashed llama-server cannot restart" |
| KV cache tak dapat dialokasikan saat mulai | "fails to allocate its KV cache when other services are starting" |
| Layanan yang dibunuh masih memegang RAM | "A killed service keeps holding its model in RAM" |
| Halaman Memory dan `free -m` berbeda | "The Memory page disagrees with `free -m`" |
| Proses mana memakai GPU? | "Which processes are on the GPU" |

## Model dan halaman Memory

| Anda melihat | Cari |
|---|---|
| Pilihan di halaman Memory tidak menetap | "Choosing a model on the Memory page does not stick" |
| Model apa yang sebenarnya dipakai? | "Which model am I actually talking to?" |
| Model yang diunduh tidak ada di daftar | "A model I downloaded is not in the list" |
| Dua pengurai berjalan, atau yang salah | "Two ASR decoders running at once" |

## Pengetahuan

| Anda melihat | Cari |
|---|---|
| Bilang tak tahu dokumen yang diberikan | "A document ingested but the robot says it does not know" |
| Benar dalam bahasa Indonesia, mengarang dalam bahasa Arab | "answers Indonesian questions from a document but invents answers in Arabic" |
| Catatan mengubah bahasa balasan | "Retrieved notes change the reply's language" |
| "Remember that …" tak berbuat apa-apa | ""Remember that …" does nothing" |
| Server embedding tak mau menyala atau bangun | "The embedding server fails to start", "The first ingestion after the embedding server wakes", "the embedding server never wakes" |

## Kamera dan orang

| Anda melihat | Cari |
|---|---|
| Tanpa gambar, atau lampu kamera kuning | "The Conversation page shows no picture" |
| Wajah tak dikenali, atau orang yang salah | "A face is not recognised" |
| Tak pernah menanyakan nama orang asing | "The robot never asks a stranger's name" |
| "Learn this face" tak berkata apa-apa | ""Learn this face" says nothing" |
| Catatan seseorang kosong | "The notes about a person come back empty" |
| Percakapan tanpa ringkasan | "A conversation has no summary" |

## Tubuh

| Anda melihat | Cari |
|---|---|
| `/dev/ttyACM0` bukan mikrokontroler | "`/dev/ttyACM0` exists but is not the microcontroller" |
| "Restart microcontroller" membuatnya terputus | ""Restart microcontroller" leaves the body disconnected" |
| Galat build `LL_GPIO_PULL_NO undeclared` | "`LL_GPIO_PULL_NO undeclared` building STM32duino" |
| Galat build `typedef struct pid_t pid_t` | "conflicting declaration" |
| Tubuhnya ESP32 dan pemeriksaan berkata "no STM32" | tidak di log: [Masalah Diketahui #19](../../../dev-docs/ai/PROGRESS.md) dan [ESP32 sebagai tubuh](../03-firmware/esp32-conformance.md) |
| Robot menggeser atau berputar ke arah yang salah | tidak di log: Masalah Diketahui #18 (selesai) dan [Gerak](../03-firmware/motion.md) |

## Dashboard dan pengawas

| Anda melihat | Cari |
|---|---|
| Semua halaman dashboard kosong | "The whole dashboard is blank" |
| Login dashboard tak pernah menetap | "The dashboard session never persists" |
| Tombol restart tak berbuat apa-apa | "The dashboard's restart buttons say nothing happened" |
| Start dilaporkan timeout padahal layanan menyala | "A service start is reported as timed out" |
| Jalan di `run.sh` tetapi rusak di systemd | "Everything works under `run.sh` and breaks under systemd" |
| Log hilang saat reboot | "Logs vanish on reboot" |
| `pkill -f` membunuh shell Anda | "`pkill -f <pattern>` kills the shell running it" |

## Jika tak ada yang cocok

1. Reproduksi sekali dan **baca `.cache/tmp/core.log` di sekitar saat itu**.
2. Periksa `./run.sh model`: dipilih, dikonfigurasi, dan berjalan harus sama.
3. Periksa `./run.sh <mode> --check` untuk pin usang dari dashboard.
4. Lihat halaman Services dan `whats_running.py` untuk swap, dan CPU di tempat yang butuh GPU.
5. Setelah ketemu, **tambahkan entri ke `TROUBLESHOOTING.md`** (gejala, penyebab, perbaikan) **dan uji regresi**, seperti diminta
   aturan 5 [`AGENTS.md`](../../../AGENTS.md). Lalu masukkan gejalanya ke indeks ini.
