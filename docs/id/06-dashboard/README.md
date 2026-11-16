# Dashboard dan panel: dua layar

> **Bahasa Indonesia.** Diterjemahkan dari [versi Inggris](../../en/06-dashboard/README.md) pada
> **2026-11-16**. Versi Inggris adalah naskah utama.

Robot punya dua layar opsional. Keduanya adalah **klien `robot-core`** dan tidak ada yang bergantung padanya:
matikan salah satu dan robot tetap mendengar, menjawab, dan bergerak.

| | Dashboard (`robot-ui`) | Panel (`robot-face`) |
|---|---|---|
| Di mana | Browser di jaringan Anda, `http://<jetson>:8081` | Layar yang terpasang pada robot |
| Untuk | Menyiapkan, men-debug, mengukur | Menampilkan percakapan dan wajah kepada orang di depannya |
| Bahasa | Python (NiceGUI sebagai server) dan HTML, CSS, JavaScript tulis tangan | **Rust dan Slint**, satu-satunya pengecualian dari "Python di Jetson" |
| Biaya | sekitar 60 MiB | **11,0 MiB** resident, plus 8,4 MiB buffer layar |
| Nyalakan dengan | `./run.sh all` | `./run.sh slint` |
| Butuh login | ya | belum ada perangkat masukan |

> **Belum ada tangkapan layar.** Halaman ini menjelaskan layar dari sumbernya. Tangkapan layar butuh tumpukan berjalan
> dengan browser terpasang, dan memori Jetson dipakai bersama tumpukan, jadi itu tugas bagi orang yang menjalankan
> robotnya.

## Halaman

| Halaman | Isi |
|---|---|
| [Dashboard, halaman demi halaman](pages.md) | Halaman masuk dan landing, lalu Dashboard, Conversation, Knowledge, Memory, Monitor, Services, Logs, Config |
| [Panel di robot](panel.md) | Yang ditampilkan `robot-face`, cara tersambung, biaya, dan yang masih terbuka |

## Aturan yang membentuk kedua layar

* **Satu bilah samping, didefinisikan sekali** (`NAV` di `static/app.js`). Dulu menambah halaman berarti menyunting sepuluh
  berkas, dan yang kesepuluh selalu terlewat.
* **`/` publik karena tidak meminta penjaga.** Itu satu-satunya halaman publik. Yang lain butuh sesi; `robot-ui`
  **menolak menyala** tanpa `ROBOT_UI_PASSWORD`.
* **Tidak ada yang melakukan polling sendiri.** Angka menumpang pada polling kesehatan 3 detik orkestrator; hanya grafik yang
  punya pencacah sendiri.
* **Komponen bersama ada di `style.css`**, tidak pernah di `<style>` halaman, dan sebuah uji (`tests/test_ui_styles.py`)
  gagal jika halaman memakai kelas yang tak didefinisikan siapa pun.
* **Satu lebar halaman, 72 rem**, dengan ukuran baca di dalamnya untuk teks.
* **Grafik adalah SVG tulis tangan**, dengan tiga warna seri yang divalidasi untuk pembaca buta warna. Glif dan label
  membawa identitas, tidak pernah warna saja, dan tidak pernah ada sumbu-y kedua.

Kabel di balik kedua layar dijelaskan di [WebSocket](../05-interfaces/websocket.md).
