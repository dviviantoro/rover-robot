# Panel di robot

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/06-dashboard/panel.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

`robot-face` adalah layar yang terpasang pada robot. Ia menampilkan percakapan, keadaan suara, pengukur level, dan ekspresi
robot. Ia **tidak** menggantikan halaman operator dashboard. Sumber: [`face.md`](../../../dev-docs/modules/face.md) dan `face/`.

```
./run.sh slint                      # semuanya + panel
./scripts/face/build.sh --check     # uji cepat: tanpa Slint, tanpa layar, robot tidak tersentuh
./scripts/face/build.sh             # build rilis untuk panel
```

## Bagaimana ia terpasang

* Ia menggambar **langsung ke perangkat keras layar** (DRM/KMS) dengan perender perangkat lunak Slint. **Tanpa desktop,
  compositor, X11, Wayland.** Batasan 5 tetap berlaku: "headless" berarti tanpa lingkungan desktop, bukan tanpa layar.
* Ia klien WebSocket orkestrator, seperti browser, dan tidak mengimpor apa pun dari sisi Python. `FACE_WS_URL`,
  `FACE_PING_S=20`, dan `FACE_DEAD_LINK_S=45` ada di `config/face.env`.
* Ia **Rust**, satu-satunya pengecualian yang disetujui (2026-09-09): binding Python Slint berhenti di versi alfa untuk
  platform dan Python 3.10 ini, dan Rust satu-satunya cara menjalankan rilis yang didukung.
* Dua crate: `face/protocol` (seluruh mesin keadaan, tanpa Slint, soket, atau jam; 27 uji yang berjalan saat robot menyala)
  dan `face/app` (UI Slint dan klien WebSocket).

## Biayanya

**11,0 MiB** resident (rilis) plus **8,4 MiB** buffer layar pada 1366×768, yang ada di kolam terpadu tetapi tidak tampak di
RSS. Dashboard 60 MiB, jadi panel dapat menggantikannya di papan dengan penghematan bersih. `MemoryMax=200M` di unitnya.

## Hal-hal yang mengejutkan

| Apa | Akibat |
|---|---|
| Layar tidak ada sampai `modprobe nvidia-drm` | Kini dipertahankan di `/etc/modules-load.d/nvidia-drm.conf`. Panel muncul sebagai `card1-DP-1`: port HDMI adalah kombo DP++, jadi nama DisplayPort untuk kabel HDMI itu wajar |
| Slint keluar saat tak ada layar terpasang | Dengan `Restart=on-failure` itu lima kali nyala ulang lalu gagal selamanya. Jadi panel **menunggu konektor** |
| Build Rust membuat LLM kena OOM kill, dua kali | Artefak build yang tercache sama tak dapat direklamasi CUDA seperti berkas model. `build.sh` menghentikan LLM selama build |
| `.cargo/config.toml` dibaca dari direktori kerja | `build.sh` mengekspor `CARGO_TARGET_DIR` alih-alih memercayainya |

## Masih terbuka ❓

* **Pembentukan huruf Arab belum diverifikasi.** Dokumentasi perender perangkat lunak berkata hanya aksara Barat; pohon
  ketergantungan berkata lain. Skrip demo memuat satu baris Arab untuk menguji. Cadangannya Noto Sans Arabic yang dibundel,
  atau perender `linuxkms-femtovg` dengan memori sekitar dua kali lipat.
* **Tanpa perangkat masukan.** Escape dan Space terhubung ke "stop" dan akan bekerja saat papan ketik atau layar sentuh ada;
  unit sudah memberi grup `input`.
* **Kunci konektor** dengan `SLINT_DRM_OUTPUT` begitu panel sungguhan terpasang (`list` mencetak yang ada).
* Ia belum terlihat pada layar robot yang sebenarnya dalam pekerjaan buku panduan ini: tidak ada tangkapan layar.

## Wajah: panel dan tubuh

**Nama** wajah dikirim apa adanya ke panel, dashboard, dan tubuh. Panel dan tubuh masing-masing menentukan cara menggambar
`blink`, `talking`, dan wajah diam. Wajah mengikuti giliran menurut aturan: `blink` untuk pengakuan, `talking` untuk jawaban,
wajah diam acak saat tak ada kejadian (lihat [Satu giliran](../01-overview/one-turn.md)). Layar tubuh
berbeda dengan panel ini: lihat [ESP32 sebagai tubuh](../03-firmware/esp32-conformance.md).
