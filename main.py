import asyncio
import json
import aiohttp
import datetime
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# --- Konfigurasi ---
INDODAX_API_URL_BASE = "https://indodax.com/api/"
FETCH_INTERVAL = 2  # Detik: Interval mengambil data dari Indodax. Amankan di 2-5 detik.
ORDER_BOOK_LIMIT = 10 # Jumlah order book (bid/ask) yang ingin dikirim

# Daftar pasangan mata uang yang akan dipantau
CURRENCY_PAIRS = [
    "BTC/IDR",
    "USDT/IDR",
    "ETH/IDR",
    "DOGE/IDR",
    "XRP/IDR",
    "ETC/IDR",
    "ADA/IDR",
    "SOL/IDR" # Menambahkan SOL/IDR, pastikan ada di HTML juga
]

# Cache untuk menyimpan data terakhir
current_data = {symbol: {} for symbol in CURRENCY_PAIRS}

# Set untuk menyimpan semua koneksi klien WebSocket yang aktif
connected_clients: set[WebSocket] = set()

# Inisialisasi aplikasi FastAPI
app = FastAPI()

# Konfigurasi untuk menyajikan file statis (HTML, CSS, JS)
# Pastikan folder 'static' ada di root direktori proyek Anda
# dan semua file front-end berada di dalamnya.
app.mount("/static", StaticFiles(directory="static"), name="static")

# Konfigurasi Jinja2Templates untuk merender HTML
# Asumsi file HTML Anda berada di dalam folder 'static'
templates = Jinja2Templates(directory="static")

async def fetch_and_broadcast_data(session: aiohttp.ClientSession, symbol: str):
    """
    Mengambil harga ticker dan depth (best bid/ask) dari Indodax untuk pasangan mata uang
    dan mengirimkannya ke semua klien WebSocket yang terhubung.
    """
    global current_data
    ticker_endpoint = f"{INDODAX_API_URL_BASE}{symbol.lower().replace('/', '_')}/ticker"
    depth_endpoint = f"{INDODAX_API_URL_BASE}{symbol.lower().replace('/', '_')}/depth"

    try:
        # 1. Fetch Ticker Data
        async with session.get(ticker_endpoint, timeout=5) as response_ticker:
            response_ticker.raise_for_status() # Akan memicu exception untuk status kode HTTP 4xx/5xx
            data_ticker = await response_ticker.json()

            if not data_ticker or 'ticker' not in data_ticker:
                print(f"Error: Struktur data ticker tidak valid dari Indodax untuk {symbol}. Respons: {data_ticker}")
                return

            ticker = data_ticker['ticker']
            new_price = float(ticker.get('last', 0.0))
            high_price = float(ticker.get('high', 0.0))
            low_price = float(ticker.get('low', 0.0))
            # Menggunakan 'vol_idr' jika ada, jika tidak, coba 'volume' atau default ke 0.0
            volume_24h = float(ticker.get('vol_idr', ticker.get('volume', 0.0)))


            percentage_change = 0.0
            # Perhitungan percentage_change yang lebih robust
            if new_price > 0 and low_price > 0:
                percentage_change = ((new_price - low_price) / low_price) * 100
            elif new_price > 0 and high_price > 0: # Fallback jika low_price 0, gunakan high_price sebagai basis
                 percentage_change = ((new_price - high_price) / high_price) * 100


        # 2. Fetch Depth Data
        buy_orders = []
        sell_orders = []

        try:
            async with session.get(depth_endpoint, timeout=5) as response_depth:
                response_depth.raise_for_status()
                data_depth = await response_depth.json()

                if 'buy' in data_depth and isinstance(data_depth['buy'], list):
                    buy_orders = sorted([[float(p), float(a)] for p, a in data_depth['buy']], key=lambda x: x[0], reverse=True)[:ORDER_BOOK_LIMIT]
                else:
                    print(f"Peringatan: Data 'buy' tidak ditemukan, kosong, atau bukan list untuk {symbol}. Respons: {data_depth}")

                if 'sell' in data_depth and isinstance(data_depth['sell'], list):
                    sell_orders = sorted([[float(p), float(a)] for p, a in data_depth['sell']], key=lambda x: x[0])[:ORDER_BOOK_LIMIT]
                else:
                    print(f"Peringatan: Data 'sell' tidak ditemukan, kosong, atau bukan list untuk {symbol}. Respons: {data_depth}")

        except aiohttp.ClientError as e:
            print(f"Error HTTP/Jaringan saat mengambil data depth dari Indodax untuk {symbol}: {e}")
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON dari Indodax API depth untuk {symbol}: {e}. Respons: {await response_depth.text() if 'response_depth' in locals() else 'N/A'}")
        except (TypeError, IndexError, ValueError) as e:
            print(f"Error parsing depth data untuk {symbol}: {e}. Pastikan struktur data sesuai yang diharapkan.")

        # Siapkan data untuk dikirim ke klien
        payload = {
            "symbol": symbol,
            "price": new_price,
            "high_price": high_price,
            "low_price": low_price,
            "percentage_change": f"{percentage_change:.2f}",
            "volume_24h": volume_24h,
            "order_book": {
                "buy_orders": buy_orders,
                "sell_orders": sell_orders
            },
            "timestamp": datetime.datetime.now().isoformat()
        }
        current_data[symbol] = payload

        best_buy_price = buy_orders[0][0] if buy_orders else 0.0
        best_sell_price = sell_orders[0][0] if sell_orders else 0.0
        print(f"Data {symbol} diambil: Terakhir=Rp {new_price:,.0f}, Tertinggi=Rp {high_price:,.0f}, Terendah=Rp {low_price:,.0f}, Perubahan={percentage_change:.2f}%, Volume 24H=Rp {volume_24h:,.0f}, Best Bid={best_buy_price:,.0f}, Best Ask={best_sell_price:,.0f}")

        message = json.dumps(payload)
        # Menggunakan set copy untuk iterasi, agar aman saat elemen dihapus (klien terputus)
        # Menggunakan send_text untuk FastAPI WebSocket
        for client_ws in list(connected_clients):
            try:
                await client_ws.send_text(message)
            except WebSocketDisconnect: # Catch specific disconnection error
                print(f"Klien {client_ws.client} terputus selama pengiriman.")
                connected_clients.remove(client_ws)
            except Exception as e:
                print(f"Error saat mengirim ke klien {client_ws.client}: {e}")
                connected_clients.remove(client_ws)

    except aiohttp.ClientError as e:
        print(f"Error HTTP/Jaringan saat mengambil data ticker dari Indodax untuk {symbol}: {e}")
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON dari Indodax API ticker untuk {symbol}: {e}. Respons: {await response_ticker.text() if 'response_ticker' in locals() else 'N/A'}")
    except TypeError as e:
        print(f"TypeError selama pemrosesan data untuk {symbol}: {e}. Periksa apakah nilai yang diharapkan adalah numerik.")
    except ValueError as e:
        print(f"ValueError selama konversi data untuk {symbol}: {e}. Sebuah nilai mungkin tidak dapat dikonversi ke tipe yang benar.")
    except Exception as e:
        print(f"Terjadi error tak terduga selama pengambilan atau penyiaran untuk {symbol}: {e}")

@app.on_event("startup")
async def startup_event():
    """Jalankan producer_task saat aplikasi startup."""
    print("Memulai producer_task (pengambilan data Indodax) di latar belakang...")
    asyncio.create_task(producer_task())

async def producer_task():
    """
    Task yang secara periodik memicu pengambilan data untuk setiap pasangan mata uang.
    Menggunakan satu aiohttp.ClientSession untuk semua request agar efisien.
    """
    async with aiohttp.ClientSession() as session:
        while True:
            # Mengambil data untuk semua pasangan mata uang secara paralel
            await asyncio.gather(*[fetch_and_broadcast_data(session, symbol) for symbol in CURRENCY_PAIRS])
            await asyncio.sleep(FETCH_INTERVAL)

# --- Endpoint HTTP (Menyajikan HTML) ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Menyajikan halaman index.html."""
    return templates.TemplateResponse("index.html", {"request": request})

# Endpoint untuk melayani file HTML di subfolder 'Pilihan-Koin 2'
# Misalnya, untuk mengakses /Pilihan-Koin 2/BTC_IDR-2.html
@app.get("/Pilihan-Koin 2/{koin_nama}.html", response_class=HTMLResponse)
async def serve_coin_detail_page(request: Request, koin_nama: str):
    """Menyajikan halaman detail koin berdasarkan nama file."""
    # Pastikan file ini ada di static/Pilihan-Koin 2/
    return templates.TemplateResponse(f"Pilihan-Koin 2/{koin_nama}.html", {"request": request})

# --- Endpoint WebSocket ---
@app.websocket("/ws") # Path WebSocket Anda
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    print(f"Klien baru {websocket.client} terhubung. Total klien: {len(connected_clients)}")
    try:
        # Kirim data terakhir yang di-cache ke klien yang baru terhubung
        for symbol in CURRENCY_PAIRS:
            if current_data[symbol]:
                await websocket.send_text(json.dumps(current_data[symbol]))
        
        # Loop ini menjaga koneksi WebSocket tetap terbuka.
        # Jika klien mengirim pesan, kita bisa memprosesnya di sini.
        # Untuk aplikasi ini, kita hanya perlu menjaga koneksi tetap hidup.
        while True:
            # Menerima pesan dari klien (jika ada).
            # Jika tidak ada pesan, ini akan menunggu, menjaga koneksi tetap hidup.
            # WebSocketDisconnect akan dipicu jika klien memutuskan koneksi.
            await websocket.receive_text() 
    except WebSocketDisconnect:
        print(f"Klien {websocket.client} terputus dengan bersih.")
    except Exception as e:
        print(f"Terjadi error pada koneksi WebSocket {websocket.client}: {e}")
    finally:
        # Pastikan klien dihapus dari set saat koneksi ditutup
        if websocket in connected_clients:
            connected_clients.remove(websocket)
            print(f"Klien {websocket.client} dihapus. Total klien: {len(connected_clients)}")

# Ini adalah bagian yang akan dijalankan ketika file ini dieksekusi secara langsung
# Saat di-deploy di Render, uvicorn akan memanggil `app` secara langsung,
# jadi blok ini tidak akan dijalankan di lingkungan Render, tapi berguna untuk testing lokal.
if __name__ == "__main__":
    import uvicorn
    # Render akan menyediakan port melalui variabel lingkungan $PORT
    # Gunakan host "0.0.0.0" agar bisa diakses dari luar
    uvicorn.run(app, host="0.0.0.0", port=8000) # Port 8000 adalah default, Render akan menggantinya
