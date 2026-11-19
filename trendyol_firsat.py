#!/usr/bin/env python3
"""
Trendyol Fırsat Botu
--------------------
Seçilen Trendyol kategorilerini tarar, her ürünün günlük fiyatını kaydeder ve
indirimi belirlenen oranı geçen ürünleri Telegram'dan bildirir.

Karma referans: ürünün en az min_gecmis_gun günlük geçmişi varsa referans
kendi geçmiş fiyatının medyanıdır; yoksa Trendyol'un üstü çizili fiyatıdır.
Böylece ilk taramadan itibaren bildirim gelir.

Kullanım:
    python trendyol_firsat.py          -> normal tarama
    python trendyol_firsat.py --test   -> sadece Telegram test mesajı gönderir

Not: Trendyol'un resmi bir API'si yoktur. Bu betik sitenin kendi arama
servisini kullanır; Trendyol bu servisi değiştirirse betik veri alamaz ve
size Telegram'dan uyarı gönderir.
"""

import html
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlparse

import requests

KONFIG_DOSYASI = "config.json"
GECMIS_DOSYASI = "veri/fiyat_gecmisi.json"
DURUM_DOSYASI = "veri/durum.json"
TR_SAATI = timezone(timedelta(hours=3))

API_ADRESI = (
    "https://public.trendyol.com/discovery-web-searchgw-service/"
    "v2/api/infinite-scroll/{yol}"
)
SITE = "https://www.trendyol.com"
BASLIKLAR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Origin": SITE,
    "Referer": SITE + "/",
}

VARSAYILAN_AYARLAR = {
    "kategoriler": [],
    "sayfa_sayisi": 15,
    "indirim_esigi": 50,
    "min_gecmis_gun": 3,
    "gecmis_gun": 30,
    "min_normal_fiyat": 100,
    "max_bildirim": 15,
}


# ----------------------------------------------------------------- yardımcılar

def log(mesaj):
    zaman = datetime.now(TR_SAATI).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{zaman}] {mesaj}", flush=True)


def json_oku(yol, varsayilan):
    try:
        with open(yol, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return varsayilan
    except json.JSONDecodeError:
        log(f"UYARI: {yol} bozuk, sıfırdan başlanıyor.")
        return varsayilan


def json_yaz(yol, veri):
    os.makedirs(os.path.dirname(yol) or ".", exist_ok=True)
    gecici = yol + ".tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(gecici, yol)


def sayi(deger):
    """Farklı biçimlerde gelen fiyatı float'a çevirir."""
    if deger is None or isinstance(deger, bool):
        return None
    if isinstance(deger, (int, float)):
        return float(deger) if deger > 0 else None
    if isinstance(deger, dict):
        for anahtar in ("value", "price", "amount"):
            if anahtar in deger:
                return sayi(deger[anahtar])
        return None
    if isinstance(deger, str):
        s = deger.replace("TL", "").replace("₺", "").replace(" ", "").strip()
        if "," in s:  # Türkçe biçim: 1.299,90
            s = s.replace(".", "").replace(",", ".")
        try:
            return sayi(float(s))
        except ValueError:
            return None
    return None


def tl(tutar):
    """1299.9 -> '1.299,90 TL'"""
    s = f"{tutar:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s + " TL"


# -------------------------------------------------------------------- telegram

def telegram_gonder(metin, onizleme=False):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    sohbet = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not sohbet:
        log("Telegram ayarları yok; mesaj yalnızca buraya yazılıyor:\n" + metin)
        return False
    adres = f"https://api.telegram.org/bot{token}/sendMessage"
    govde = {
        "chat_id": sohbet,
        "text": metin,
        "parse_mode": "HTML",
        "disable_web_page_preview": not onizleme,
    }
    for _ in range(3):
        try:
            r = requests.post(adres, json=govde, timeout=20)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                bekle = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(int(bekle) + 1)
                continue
            log(f"Telegram hatası {r.status_code}: {r.text[:300]}")
            return False
        except requests.RequestException as e:
            log(f"Telegram bağlantı hatası: {e}")
            time.sleep(3)
    return False


# ---------------------------------------------------------------------- tarama

def sayfa_getir(oturum, yol, parametreler, sayfa):
    params = dict(parametreler)
    params.update({"pi": sayfa, "culture": "tr-TR"})
    adres = API_ADRESI.format(yol=yol)
    for deneme in range(3):
        try:
            r = oturum.get(adres, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
            log(f"  {yol} sayfa {sayfa}: HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            log(f"  {yol} sayfa {sayfa}: {e}")
        time.sleep(4 * (deneme + 1))
    return None


def urunleri_ayikla(veri):
    sonuc = (veri or {}).get("result") or (veri or {}).get("data") or {}
    ham = sonuc.get("products") or []
    urunler = []
    for u in ham:
        try:
            uid = u.get("id") or u.get("contentId")
            if not uid:
                continue
            fiyat_blok = u.get("price") or {}
            if not isinstance(fiyat_blok, dict):
                fiyat_blok = {"sellingPrice": fiyat_blok}
            adaylar = [
                sayi(fiyat_blok.get(k))
                for k in ("discountedPrice", "sellingPrice", "price")
            ]
            adaylar = [a for a in adaylar if a]
            if not adaylar:
                continue
            fiyat = min(adaylar)
            liste = sayi(fiyat_blok.get("originalPrice"))
            if liste is not None and liste <= fiyat:
                liste = None

            marka = u.get("brand")
            if isinstance(marka, dict):
                marka = marka.get("name")
            ad = " ".join(x for x in (marka, u.get("name")) if x).strip()

            url = u.get("url") or ""
            if url and not url.startswith("http"):
                url = SITE + url

            urunler.append({
                "id": str(uid),
                "ad": ad or f"Ürün {uid}",
                "url": url or f"{SITE}/x-p-{uid}",
                "fiyat": round(fiyat, 2),
                "liste": round(liste, 2) if liste else None,
            })
        except Exception as e:  # tek bir ürün tüm taramayı bozmasın
            log(f"  ürün atlandı: {e}")
    return urunler


def aktif_kategoriler(ayarlar):
    return [k for k in ayarlar["kategoriler"] if k.get("aktif", True) and k.get("url")]


def tara(ayarlar):
    oturum = requests.Session()
    oturum.headers.update(BASLIKLAR)
    tum = {}
    for kat in aktif_kategoriler(ayarlar):
        adres = urlparse(kat["url"])
        yol = adres.path.strip("/")
        parametreler = dict(parse_qsl(adres.query))
        adet = 0
        for sayfa in range(1, int(ayarlar["sayfa_sayisi"]) + 1):
            veri = sayfa_getir(oturum, yol, parametreler, sayfa)
            urunler = urunleri_ayikla(veri)
            if not urunler:
                break
            for u in urunler:
                u["kategori"] = kat.get("ad", yol)
                tum[u["id"]] = u
            adet += len(urunler)
            time.sleep(random.uniform(1.5, 3.0))
        log(f"{kat.get('ad', yol)}: {adet} ürün okundu")
    return tum


# ---------------------------------------------------------------- değerlendirme

def degerlendir(urunler, gecmis, durum, ayarlar, bugun):
    esik = float(ayarlar["indirim_esigi"]) / 100
    bildirilen = durum.setdefault("bildirilen", {})
    firsatlar = []

    for uid, u in urunler.items():
        kayit = gecmis.setdefault(uid, {"p": {}})
        kayit["n"], kayit["u"] = u["ad"], u["url"]

        onceki = [v for gun, v in kayit["p"].items() if gun != bugun]
        if len(onceki) >= int(ayarlar["min_gecmis_gun"]):
            normal, kaynak = statistics.median(onceki), "gecmis"
        elif u.get("liste"):
            normal, kaynak = u["liste"], "liste"
        else:
            normal, kaynak = None, None

        if normal:
            sinir = normal * (1 - esik)
            if normal >= float(ayarlar["min_normal_fiyat"]) and u["fiyat"] <= sinir:
                eski_bildirim = bildirilen.get(uid)
                # Aynı ürünü, fiyat %3'ten fazla düşmedikçe tekrar bildirme
                if eski_bildirim is None or u["fiyat"] < eski_bildirim * 0.97:
                    firsatlar.append({
                        **u,
                        "normal": normal,
                        "indirim": round((1 - u["fiyat"] / normal) * 100),
                        "gun": len(onceki),
                        "kaynak": kaynak,
                    })
            elif uid in bildirilen and u["fiyat"] > sinir:
                del bildirilen[uid]  # fiyat normale döndü; ileride yeniden bildirilebilir

        mevcut = kayit["p"].get(bugun)
        kayit["p"][bugun] = u["fiyat"] if mevcut is None else min(mevcut, u["fiyat"])

    firsatlar.sort(key=lambda f: f["indirim"], reverse=True)
    return firsatlar


def gecmisi_kirp(gecmis, durum, ayarlar, bugun_tarih):
    sinir = (bugun_tarih - timedelta(days=int(ayarlar["gecmis_gun"]))).isoformat()
    for uid in list(gecmis):
        gunler = {g: v for g, v in gecmis[uid]["p"].items() if g >= sinir}
        if gunler:
            gecmis[uid]["p"] = gunler
        else:
            del gecmis[uid]
            durum.get("bildirilen", {}).pop(uid, None)


def referans_satiri(f):
    if f.get("kaynak") == "gecmis":
        satir = f"Normalde: ~{tl(f['normal'])} (son {f['gun']} günün medyanı)"
        if f.get("liste"):
            satir += f"\nÜstü çizili: {tl(f['liste'])}"
        return satir
    return (f"Üstü çizili: {tl(f['normal'])}\n"
            "<i>Fiyat geçmişi henüz yok; üstü çizili fiyat şişirilmiş olabilir.</i>")


def firsat_mesaji(f):
    return (
        f"🔥 <b>%{f['indirim']} indirim</b> · {html.escape(f['kategori'])}\n"
        f"{html.escape(f['ad'][:200])}\n\n"
        f"Şimdi: <b>{tl(f['fiyat'])}</b>\n"
        f"{referans_satiri(f)}\n"
        f"<a href=\"{html.escape(f['url'], quote=True)}\">Ürüne git</a>"
    )


# ------------------------------------------------------------------------ ana

def main():
    ayarlar = {**VARSAYILAN_AYARLAR, **json_oku(KONFIG_DOSYASI, {})}

    if "--test" in sys.argv:
        ok = telegram_gonder(
            "✅ Trendyol fırsat botu bağlandı.\n"
            f"İndirim eşiği: %{ayarlar['indirim_esigi']}\n"
            f"Kategoriler: "
            f"{', '.join(html.escape(k.get('ad', '?')) for k in aktif_kategoriler(ayarlar))}"
        )
        log("Test mesajı gönderildi." if ok else "Test mesajı GÖNDERİLEMEDİ.")
        sys.exit(0 if ok else 1)

    if not aktif_kategoriler(ayarlar):
        log("config.json içinde açık kategori yok.")
        sys.exit(1)

    gecmis = json_oku(GECMIS_DOSYASI, {})
    durum = json_oku(DURUM_DOSYASI, {})
    simdi = datetime.now(TR_SAATI)
    bugun = simdi.date().isoformat()

    urunler = tara(ayarlar)
    durum["son_calisma"] = simdi.isoformat(timespec="minutes")

    if not urunler:
        log("Hiç ürün okunamadı.")
        if not durum.get("hata_bildirildi"):
            telegram_gonder(
                "⚠️ Trendyol'dan veri alınamadı. Trendyol erişimi engellemiş ya da "
                "servisini değiştirmiş olabilir. Sorun sürerse bu mesaj tekrar gelmez; "
                "GitHub'daki Actions kayıtlarına bakabilirsin."
            )
            durum["hata_bildirildi"] = True
        json_yaz(DURUM_DOSYASI, durum)
        return

    if durum.get("hata_bildirildi"):
        telegram_gonder("✅ Trendyol taraması yeniden çalışıyor.")
    durum["hata_bildirildi"] = False

    firsatlar = degerlendir(urunler, gecmis, durum, ayarlar, bugun)
    gecmisi_kirp(gecmis, durum, ayarlar, simdi.date())

    limit = int(ayarlar["max_bildirim"])
    for f in firsatlar[:limit]:
        if telegram_gonder(firsat_mesaji(f), onizleme=True):
            durum["bildirilen"][f["id"]] = f["fiyat"]
        time.sleep(1)
    if len(firsatlar) > limit:
        telegram_gonder(
            f"ℹ️ {len(firsatlar) - limit} fırsat daha var; sonraki taramada gönderilecek."
        )

    log(f"{len(urunler)} ürün işlendi, {len(firsatlar)} fırsat bulundu, "
        f"geçmişte {len(gecmis)} ürün var.")
    json_yaz(GECMIS_DOSYASI, gecmis)
    json_yaz(DURUM_DOSYASI, durum)


if __name__ == "__main__":
    main()
