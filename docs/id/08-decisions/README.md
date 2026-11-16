# Keputusan: mengapa dibangun seperti ini

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/08-decisions/README.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

Robot yang berjalan di memori bersama 8 GB adalah jumlah dari banyak "tidak". Bab ini mencatat **apa yang diputuskan,
mengapa, apa yang ditolak, dan apa yang masih menunggu keputusan seseorang**, agar tidak seorang pun harus menemukannya
lagi atau memperdebatkannya tanpa informasi baru.

| Halaman | Isi |
|---|---|
| [Batasan keras](../../en/08-decisions/hard-constraints.md) *(Inggris)* | Delapan aturan yang butuh persetujuan manusia untuk dilanggar, dengan alasan dan buktinya |
| [Alternatif yang ditolak dan ditunda](../../en/08-decisions/rejected-alternatives.md) *(Inggris)* | Yang dipertimbangkan dan tidak dipilih, dengan angkanya, dan kondisi untuk melihatnya lagi |
| [Menunggu pemilik proyek](open-decisions.md) | Pertanyaan terbuka dan catatan yang sudah dikoreksi |

## Catatan lengkap ada di mana

Buku panduan merangkum; ini sumbernya, dan merekalah yang menang jika berbeda dengan halaman di sini.

| Catatan | Isi |
|---|---|
| [`AGENTS.md`](../../../AGENTS.md) | Kontrak: batasan keras, aturan kerja, dan penalaran terukur di balik yang terpenting |
| [`planning/PROJECT_BRIEF.md`](../../../planning/PROJECT_BRIEF.md) | Yang dibangun, bukan-tujuan, **alternatif yang ditolak**, kriteria sukses |
| [`planning/TECH_STACK.md`](../../../planning/TECH_STACK.md) | Setiap ketergantungan, mengapa dipilih, dan biayanya |
| [`dev-docs/ai/PROGRESS.md`](../../../dev-docs/ai/PROGRESS.md) bagian 4 | **Log keputusan**: satu baris bertanggal untuk tiap keputusan |
| [`dev-docs/ai/TROUBLESHOOTING.md`](../../../dev-docs/ai/TROUBLESHOOTING.md) | Keputusan yang lahir dari kegagalan: gejala, penyebab, perbaikan |

## Empat kebiasaan yang berulang

* **Ukur, jangan asumsikan.** Suara CPU lawan GPU, peredam derau, ukuran model embedding yang sebenarnya (perkiraan
  250 MB, terukur 630 MB): tiap keputusan membawa pengukuran yang melahirkannya.
* **Gagal dengan keras, bukan diam-diam.** `WHISPER_REQUIRE_GPU` dan `TTS_REQUIRE_GPU` ada karena mesin suara yang
  menyala di CPU tidak berkata apa-apa dan robot "hanya menjadi lebih lambat".
* **Bawaannya mode aman, yang berisiko adalah pengaturan.** Mendengar bawaannya `no_interrupt` karena menyela membuat
  robot menjawab dirinya sendiri; peredam derau mati karena kalah dalam uji.
* **Teks biasa dan satu pemilik.** Protokol serial adalah teks, soket layanan adalah teks, dan hanya `robot-core` yang
  berbicara dengan tubuh.
