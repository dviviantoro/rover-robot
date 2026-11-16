# JSON balasan: yang dijawab model, dan yang dikirim ke tubuh

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/05-interfaces/reply-json.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

Robot menjawab dengan **objek JSON**, bukan prosa dengan penanda di dalamnya. Hanya satu bidang yang diucapkan; sisanya
menyatakan apa yang dilakukan tubuh *selagi* ia berbicara.

```json
{"answer": "Baik, saya maju sedikit.", "language": "id",
 "foot_direction": "forward", "foot_step": 2,
 "hand_movement": "none", "face": "happy", "style": "cheerful"}
```

Sumber: [`src/robot/core/reply.py`](../../../src/robot/core/reply.py).

## Tujuh bidang

| Bidang | Tipe dan nilai | Yang terjadi padanya |
|---|---|---|
| `answer` | string | **Satu-satunya bidang yang diucapkan.** Kalimat biasa, tanpa markup, tanpa emoji |
| `language` | `id`, `en`, atau `ar` | Pendapat model sendiri. Bahasa yang dipakai diputuskan dari kata-kata orang itu (lihat [Alur suara](../04-brain/voice-pipeline.md)) |
| `foot_direction` | `none`, `forward`, `backward`, `left`, `right`, `turn_left`, `turn_right` | Menjadi satu `DRIVE` |
| `foot_step` | bilangan bulat 0 sampai 10 | Berapa lama: satu langkah 600 ms. Dijepit ke 0 sampai 10 |
| `hand_movement` | `none`, `wave`, `point`, `home`, `nod` | Menjadi satu `HAND` |
| `face` | `neutral`, `happy`, `thinking`, `listening`, `speaking`, `sad` | Di bawah aturan fase bawaan, wajah ditentukan fase, bukan bidang ini |
| `style` | `neutral`, `cheerful`, `calm`, `excited`, `apologetic` | Dibawa ke dashboard dan tubuh. **Suara tidak menindaklanjutinya** |

Satu langkah adalah `STEP_MS = 600` pada `STEP_SPEED = 0,2` m/s, sekitar 12 cm; langkah belok 1,2 rad/s, sekitar 41°.
Sepuluh langkah paling banyak yang dapat diminta, 6 detik.

Bagaimana arah menjadi angka (satu langkah satuan sebelum diskala 0,2 m/s), dari `_STEP_VECTORS` di `turn.py`:

| `foot_direction` | vx | vy | omega |
|---|---|---|---|
| `forward` / `backward` | +1 / −1 | 0 | 0 |
| `left` / `right` | 0 | +1 / −1 | 0 |
| `turn_left` / `turn_right` | 0 | 0 | +1,2 / −1,2 |

(Kerangkanya sudah [ditetapkan](../03-firmware/motion.md): `+vy` kiri, `+omega` berlawanan arah jarum jam.)

## Mengapa dibangun begini

* **Bentuknya dipaksakan, bukan diminta.** llama.cpp diberi skema JSON dan tak dapat menghasilkan hal lain: bukan prosa, bukan
  JSON setengah jadi, bukan bidang karangan. Meminta penanda dalam teks bebas gagal sekitar satu dari dua puluh giliran.
  `SCHEMA` di `reply.py` adalah seluruh kontraknya.
* **`answer` adalah properti pertama, dan itu menentukan.** Tata bahasa menghasilkan properti menurut urutan skema, jadi kalimat
  yang diucapkan ada sebelum bidang tubuh, dan `AnswerStream` membaca jawaban dari aliran token *saat ditulis*. Menunggu kurung
  tutup akan menambahkan seluruh waktu pembangkitan ke jeda sebelum kata pertama.
* **Kosakata yang tak dikenal tubuh adalah perintah yang dijawab `ERR`.** Enum di `reply.py` dijaga selaras dengan wajah dan gestur
  tubuh karena itu.

## Bila ada yang salah

| Keadaan | Yang dilakukan robot |
|---|---|
| Balasan bukan JSON yang terbaca | **Mengucapkan teks mentah** sebagai jawaban, tanpa aksi tubuh. Lebih baik berkata sesuatu daripada diam |
| Bidang bernilai di luar daftarnya | Bidang itu kembali ke nilai netralnya (`none`, `neutral`, tanpa bahasa) |
| `foot_step` bukan angka atau di luar rentang | Menjadi 0, atau dijepit ke 0 sampai 10 |
| Balasan menyentuh batas token di tengah jawaban | Objek tak pernah tertutup, jadi `language`, `foot_direction`, `foot_step`, `hand_movement`, `face`, dan `style` **tak pernah dihasilkan**: robot mengucapkan setengah kalimat dan tidak bergerak. Kini ia mengatakannya: satu PERINGATAN, peristiwa `truncated` di dashboard, dan bendera `truncated` pada angka giliran |
| Bahasa Arab atau emoji di `answer` | Escape `\uXXXX` didekode, dan pasangan pengganti digabung. Separuh yatim dibuang karena tak dapat dikodekan dan pernah mengakhiri giliran di tengah balasan |

Format balasan dapat diganti dengan `CORE_REPLY_FORMAT`: `json` (bawaan) atau `markers`, bentuk lama prosa dengan `[do:...]`,
yang tak butuh apa-apa dari server selain streaming.

## Baris yang diterima tubuh

Sekali per fase giliran, `robot-core` juga menulis objek yang sama ke tubuh sebagai **satu baris JSON ASCII ringkas**, dengan
tambahan `phase`. Ia diawali `{`, sehingga tak bisa tertukar dengan kata kerja.

| `phase` | Kapan | `answer` | `face` |
|---|---|---|---|
| `acknowledgement` | "baik, sebentar" lisan dimulai (`CORE_FILLER=1`) | kalimatnya | `blink` |
| `answering` | balasan model lengkap | yang diucapkan | `talking` |
| `idle` | giliran selesai, dan sekali saat mulai dan tiap sambung ulang | kosong | `random` |

```
{"phase":"acknowledgement","answer":"Oke, sebentar ya.","language":"id","foot_direction":"none","foot_step":0,"hand_movement":"none","face":"blink","style":"neutral"}
{"phase":"answering","answer":"Halo! Ada yang bisa saya bantu?","language":"id","foot_direction":"none","foot_step":0,"hand_movement":"none","face":"talking","style":"neutral"}
{"phase":"idle","answer":"","language":"id","foot_direction":"none","foot_step":0,"hand_movement":"none","face":"random","style":"neutral"}
```

* Baris **paling banyak 255 byte**. Kunci selain `answer` memakai paling banyak 164 di antaranya, jadi `answer` satu-satunya bidang
  yang pernah dipangkas dan setiap bidang tubuh selalu selamat.
* **Hanya ASCII** (`\uXXXX` untuk yang lain), sehingga jawaban Arab tak dapat merusak penulisan.
* Setiap fase juga mengirim perintah `FACE <wajah>` yang sesuai, untuk tubuh yang belum membaca JSON.
* **Tidak ada yang menunggunya, dan tubuh tidak menjawabnya.** Tubuh yang menjawab `ERR {"…` diabaikan. Tubuh tetap harus
  menganggapnya baris untuk watchdog.
* Dengan `CORE_FILLER=0` tidak ada pengakuan dan karenanya tak ada baris itu. Di bawah `CORE_FACE_MODE=model` hanya `answering` dikirim.
* `grep 'robot-serial: reply' .cache/tmp/core.log` menjawab "apakah tubuh menerimanya".

**Firmware harus dibangun dengan `PROTOCOL_MAX_LINE 256`.** Build lama (96) memotong baris dan mengurai ekornya sebagai perintah
sendiri. Begitulah biner simulator usang pernah gagal.
