# Operasional: memasang, menjalankan, dan memperbaiki

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/07-operations/README.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama: jika berbeda, ikuti versi Inggris.

Bab ini untuk orang yang harus **membuat robot berjalan** dan **menjaganya tetap berjalan**:
menyiapkan papan, menyalakan seluruh layanan, membiarkannya, dan mencari tahu apa yang salah saat
robot bertingkah aneh.

## Dua cara menjalankan

Layanan yang sama berjalan di bawah kedua pengawas, membaca `config/*.env` yang sama. Tingkat
pembuktiannya **tidak sama**.

| | `./run.sh` (tumpukan pengembangan) | systemd (`deploy/systemd/`, cara robot seharusnya dikirim) |
|---|---|---|
| Menyalakan | layanan, berurutan, sebagai pengguna Anda | sembilan unit di bawah `robot.target` |
| Menyalakan ulang layanan yang mogok | **tidak**: *keepalive* milik `robot-core` yang melakukannya | ya: `Restart=on-failure` |
| Isolasi | tidak ada | `ProtectSystem=strict`, `PrivateTmp`, batas memori |
| Keadaan | ✅ dipakai setiap hari | ❓ **belum pernah dipasang atau di-boot di papan ini**; dibaca, diperiksa dengan `systemd-analyze verify`, dan empat kesalahan ditemukan serta diperbaiki dengan membaca, 2026-10-22 |

**Perubahan pada sebuah layanan belum selesai sebelum dibaca di bawah keduanya.** Keduanya membaca
berkas env dengan cara berbeda (unit mempertahankan `# komentar` di akhir nilai; bash tidak), dan
hanya systemd yang mengisolasi. Lihat [systemd](../../en/07-operations/systemd.md) *(Inggris)*.

## Halaman

| Halaman | Baca jika |
|---|---|
| [Memasang di papan baru](install.md) | Anda punya Jetson dan salinan kode, tetapi belum ada yang berjalan |
| [Menjalankan sehari-hari](running.md) | Anda ingin menyalakan, menghentikan, melihat, atau mengukur robot |
| [Produksi dengan systemd](../../en/07-operations/systemd.md) *(Inggris)* | Robot akan dibiarkan menyala tanpa pengawasan |
| [Indeks gejala](../../en/07-operations/troubleshooting-index.md) *(Inggris)* | Robot "tuli", bisu, lambat, atau macet, atau dashboard kosong |

## Keadaan papan ini pada 2026-11-16 ✅

Dari `./scripts/bringup/check_board.sh` (hanya membaca; tidak mengubah apa pun), dijalankan hari ini:

| Pemeriksaan | Hasil |
|---|---|
| JetPack | 6.2.1 (L4T 36.4.7), mode daya **MAXN_SUPER**, tanpa layar desktop (`multi-user.target`) |
| `jtop` | terpasang dan aktif |
| Penyimpanan | repo dan model di NVMe, **225 GB kosong**; **sistem berkas root terpakai 96%, sisa 4 GB** |
| Unit | kesembilannya menyatakan `Restart=on-failure` |
| Memori | total 7619 MiB, terpakai 969 MiB dengan **tumpukan berhenti** |
| Keseluruhan | **11 lulus, 0 gagal** |
| Peringatan | tidak ada mikrofon dan speaker USB yang terlihat *saat ini*; tidak ada `/dev/ttyACM*` (tubuh belum terpasang); jurnal bersifat sementara (volatile) |

## Dua hal yang akan menjebak Anda

1. **Sistem berkas root hampir penuh** (96% hari ini). Model, hasil build, cache pip, dan image
   Docker harus ada di partisi DATA. Selalu jalankan `source scripts/bringup/env.sh` sebelum
   `pip install` atau mengunduh apa pun.
2. **Memori papan adalah batas atasnya.** RAM yang bisa dipakai **7620 MiB, bukan 8192**. Tumpukan
   penuh mencapai sekitar 6,5 GB. **Jangan menjalankan tumpukan penuh bersamaan dengan sesi
   pengembangan**: bersama-sama keduanya terukur **97% terpakai dan 3,2 GB swap**. Swap mengubah suara
   0,6 detik menjadi 10 detik.

Satu lagi, untuk tubuh robot: **`/dev/ttyACM0` di papan ini pernah menjadi kartu suara USB**, bukan
mikrokontroler. Jangan pernah memanggil tubuh dengan nomor `ttyACM`.
