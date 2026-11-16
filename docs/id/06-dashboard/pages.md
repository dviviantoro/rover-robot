# Dashboard, halaman demi halaman

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/06-dashboard/pages.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

Urutan bilah samping: **Dashboard, Conversation, Knowledge**, lalu grup **System** (**Memory, Monitor, Services**), lalu
**Logs** dan **Config**. Titik di bawah bilah samping menunjukkan apakah browser tersambung ke orkestrator. Sumber:
`src/robot/ui/static/*.html` dan [`AGENTS.md`](../../../AGENTS.md) bagian The dashboard.

## Sebelum halaman-halaman

| Halaman | Alamat | Isi |
|---|---|---|
| **Landing** | `/` | **Publik.** Apa itu robot, untuk orang yang baru diberi alamat: tiga bahasa, berjalan sepenuhnya di robot, tanpa cloud. Tombol "Open dashboard" |
| **Masuk** | `/login` | Formulir biasa dan cookie bertanda tangan. Penggunanya `ROBOT_UI_USER` (bawaan `admin`) |
| (alamat lama) | `/chat`, `/headless`, `/vision` | Dialihkan ke Conversation, yang menggantikannya |

## Dashboard (`/dashboard`)

*Ringkasannya.* Empat angka utama: **memori terpakai** dari 7620 MiB, **potongan pengetahuan** tersimpan, **token masuk**
(prompt) dan **token keluar** (dihasilkan). Di bawahnya: **aktivitas papan** (grafik yang diambil tiap 2 detik, jadi menit
sebelum Anda membuka halaman pun ada), **token per giliran**, kartu **memori** kecil, kartu **sistem** (apa yang
dikonfigurasi dan apa yang berjalan) dan **percakapan terbaru**.

Hitungan token adalah milik **llama.cpp sendiri**, bukan perkiraan. Bagian "cached" penting: persona dikirim tiap giliran dan
hampir seluruhnya dipakai ulang, tanpanya prompt tampak jauh lebih mahal. Total seumur hidup di-reset saat `robot-core`
dinyalakan ulang; tidak ada penyimpanan di baliknya.

## Conversation (`/talk`)

*Halaman yang dibuka saat sebuah giliran terasa lambat.* Satu layar tanpa gulir halaman: **utas** di kiri, diketik dan diucapkan
bersama; di kanan **mata robot** (gambar kamera, dengan legenda untuk "orang yang dikenalnya", "wajah yang tidak dikenal" dan
"objek", dan daftar **Objek** dan **Wajah**) dan log **Activity**, alur giliran demi giliran dengan JSON tiap balasan.

* Kotak **Send** dan sakelar **Read answers aloud**: giliran yang diketik senyap kecuali sakelar menyala.
* **Nobody recognised** menunjukkan siapa yang dilihat kamera. Saat robot menanyakan nama orang asing, halaman
  mengatakannya dan menawarkan *No thanks*.
* Jalur proses menunjukkan **hearing, transcribing, retrieving, thinking, speaking, serial**.
* Setiap giliran lisan punya rincian **Timing** yang bisa dilipat: durasi per tahap dengan yang paling lambat ditandai dan
  pengaturan yang mengaturnya disebut. Durasinya **berjumlah persis total**, itulah yang membuat tabel dapat dipercaya.
* **Tidak ada Stop atau Clear** untuk percakapan.

Lihat [Satu giliran](../01-overview/one-turn.md) untuk arti tiap tahap.

## Knowledge (`/knowledge`)

**System prompt** (persona) dan dokumen yang dinalarnya, dalam satu halaman.

* **System prompt:** *Save* dan *Revert*. Ia pengetahuan, bukan kenop penyetel: halaman menunjukkan biayanya, kira-kira
  seperempat jumlah karakternya dalam token, dikirim ulang tiap giliran.
* **Documents:** sumber dan jumlah potongan; klik satu untuk tiga tampilan: **Document**, **Chunks (JSON)**, dan **About**
  (istilah kunci, dan atas permintaan ringkasan dan entitas dari LLM yang sedang dimuat).
* **Add knowledge:** jatuhkan berkas (`.txt .md .json .csv .log .rst .yaml`, sampai 8 MB teks UTF-8), ketik sebuah fakta,
  atau beri jalur berkas di robot (hanya di bawah `run/uploads/` atau `knowledge/`).

Rinciannya ada di [Pengetahuan](../04-brain/knowledge.md).

## Memory (`/memory`)

*Menghitung biaya perubahan model sebelum diterapkan.* Setiap baris menunjukkan **Estimated** di samping **Measured now**:
perkiraan adalah ukuran disk dikali faktor terukur, pengukuran adalah RSS proses dari `/proc`, disegarkan tiap 3 detik.
**Jika berbeda, pengukuranlah yang benar.**

* Ringkasan: memori terpakai dari 7620 MiB saat puncak, **steady state**, **loaded now**, **mid-turn peak**, **headroom**.
* **Model selection** (LLM, model pendengar, suara, sakelar GPU) menghitung ulang saat Anda mengubahnya. Menerapkannya
  adalah muat ulang, bukan nyala ulang. **Installed models** mendaftar yang ada di disk; berkas baru muncul dalam sekitar 3 detik.

## Monitor (`/monitor`)

Papan seperti sekarang: memori terpakai, **swap** ("swap di papan ini berarti thrashing: merusak latensi"), daya dan suhu,
dan tabel per layanan dengan tren. RSS melebih-lebihkan model karena menghitung bobot yang dipetakan memori, yang dapat
dikeluarkan kernel; **perhatikan satu baris yang naik sendiri: itulah rupa kebocoran sebelum menjadi OOM kill.**

## Services (`/services`)

Setiap proses penyusun robot, beratnya, dan tombol **start, stop, restart** yang bekerja lewat pengawas mana pun yang
berkuasa (`run_stack.sh` atau systemd). Menghentikan satu adalah cara membebaskan memori untuk yang lain. Memulai bisa gagal
walau memori kosong: layanan pemuat model butuh memori *berurutan* yang tak dapat diambil CUDA di Tegra dari proses lain,
jadi hentikan sesuatu dulu. Setiap aksi dicatat beserta siapa yang meminta.

## Logs (`/logs`)

* **Orchestrator (live)** dan satu tab per log layanan (tab yang diredupkan adalah layanan yang tidak berjalan; lognya tetap
  tempat alasannya).
* **Conversations**: setiap percakapan robot dengan orang yang dikenalnya, per orang, dengan ringkasannya. Ini data pribadi
  dan tetap di robot.
* Tombol level (Debug, Info, Warn, Error), Pause, Clear, dan **log level** orkestrator. Debug mencatat balasan mentah model
  sebelum dipangkas.

## Config (`/config`)

*Semua yang diekspos robot, dapat disetel tanpa SSH.* Pengaturan langsung berlaku seketika; sisanya menyalakan ulang satu
komponen dan mengatakannya. Tab mengikuti grup di registri: **Hardware** (mikrofon, speaker, volume, ambang pendeteksi suara,
gerbang derau, peredam derau, **port mikrokontroler** dan *Ping all ports*), **Listening**, **Thinking**, **Knowledge**
(berapa potongan diambil), **Speaking**, dan **People**. Kontrol model dan persona ada di Memory dan Knowledge.

* **Perangkat yang berpindah tetap di daftar pilihan, terpilih, dengan alasannya di sampingnya**, jadi halaman menunjukkan
  apa yang salah.
* Sumber monitor tidak pernah ditawarkan sebagai mikrofon: merekamnya berarti merekam suara robot sendiri.
* Sakelar peredam derau membawa pengukuran yang membuatnya bawaan mati.

Daftar lengkap 32 pengaturan, dengan rentang dan bawaan, ada di [Konfigurasi](../05-interfaces/configuration.md).
