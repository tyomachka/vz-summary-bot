"""Daily VZ summary pipeline.

Fetches RSS, filters by section, logs into vz.lt, scrapes full article bodies,
sends them to Gemini for structured extraction, validates source quotes against
the article text, renders HTML, and emails the summary.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import smtplib
import sys
import time
import unicodedata
import traceback
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import trafilatura
from google import genai
from google.genai import errors as gerrors
from google.genai import types
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

# Config
RSS_URL = "https://www.vz.lt/rss"
LOGIN_URL = "https://prisijungimas.vz.lt/verslo-zinios"
HOMEPAGE = "https://www.vz.lt/"
MAX_ARTICLES = 18
ARTICLE_FETCH_TIMEOUT_MS = 30_000
GEMINI_MODEL = "gemini-2.5-flash"
# Tried in order on sustained failures (503 / quota exhaustion).
_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]

# Static company → ticker map. Keys are lowercased substrings as they typically
# appear in VŽ articles. Lookup is case-insensitive substring containment, so
# "AB Apranga" matches "apranga". Used in validate(); never sent to Gemini.
# Extend this dict over time as new names appear.
TICKER_MAP: dict[str, dict] = {
    # --- Nasdaq Baltic Main + Secondary (Lithuania) ---
    "apranga": {"ticker": "APG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "ignitis grupė": {"ticker": "IGN1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "ignitis": {"ticker": "IGN1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "telia lietuva": {"ticker": "TEL1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "artea bankas": {"ticker": "ROE1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "šiaulių bankas": {"ticker": "ROE1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "siauliu bankas": {"ticker": "ROE1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "auga group": {"ticker": "AUG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "grigeo": {"ticker": "GRG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "klaipėdos nafta": {"ticker": "KNE1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "klaipedos nafta": {"ticker": "KNE1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "kn energies": {"ticker": "KNE1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "panevėžio statybos trestas": {"ticker": "PTR1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "pieno žvaigždės": {"ticker": "PZV1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "rokiškio sūris": {"ticker": "RSU1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "snaigė": {"ticker": "SNG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "vilkyškių pieninė": {"ticker": "VLP1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "litgrid": {"ticker": "LGD1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "akropolis group": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    # --- Nasdaq Baltic (Latvia) ---
    "citadele": {"ticker": "CBL", "exchange": "Nasdaq Riga", "country": "LV", "cap": "mid"},
    "latvijas gāze": {"ticker": "GZE1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "small"},
    "olainfarm": {"ticker": "OLF1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "small"},
    "sigulda": {"ticker": "SCM1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "micro"},
    # --- Nasdaq Baltic (Estonia) ---
    "tallink": {"ticker": "TAL1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "tallinna kaubamaja": {"ticker": "TKM1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "lhv group": {"ticker": "LHV1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "mid"},
    "coop pank": {"ticker": "CPA1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "enefit green": {"ticker": "EGR1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "mid"},
    "tallinna sadam": {"ticker": "TSM1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "mid"},
    "harju elekter": {"ticker": "HAE1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "merko ehitus": {"ticker": "MRK1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "tallinna vesi": {"ticker": "TVEAT", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    # --- US mega/large caps frequently in VŽ ---
    "apple": {"ticker": "AAPL", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "microsoft": {"ticker": "MSFT", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "alphabet": {"ticker": "GOOGL", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "google": {"ticker": "GOOGL", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "amazon": {"ticker": "AMZN", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "meta": {"ticker": "META", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "facebook": {"ticker": "META", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "nvidia": {"ticker": "NVDA", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "tesla": {"ticker": "TSLA", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "berkshire hathaway": {"ticker": "BRK.B", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "jpmorgan": {"ticker": "JPM", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "bank of america": {"ticker": "BAC", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "goldman sachs": {"ticker": "GS", "exchange": "NYSE", "country": "US", "cap": "large"},
    "morgan stanley": {"ticker": "MS", "exchange": "NYSE", "country": "US", "cap": "large"},
    "exxon": {"ticker": "XOM", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "chevron": {"ticker": "CVX", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "boeing": {"ticker": "BA", "exchange": "NYSE", "country": "US", "cap": "large"},
    "lockheed martin": {"ticker": "LMT", "exchange": "NYSE", "country": "US", "cap": "large"},
    "raytheon": {"ticker": "RTX", "exchange": "NYSE", "country": "US", "cap": "large"},
    "palantir": {"ticker": "PLTR", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "ebay": {"ticker": "EBAY", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "gamestop": {"ticker": "GME", "exchange": "NYSE", "country": "US", "cap": "small"},
    "amd": {"ticker": "AMD", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "intel": {"ticker": "INTC", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "netflix": {"ticker": "NFLX", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "disney": {"ticker": "DIS", "exchange": "NYSE", "country": "US", "cap": "large"},
    "uber": {"ticker": "UBER", "exchange": "NYSE", "country": "US", "cap": "large"},
    "coca-cola": {"ticker": "KO", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "pfizer": {"ticker": "PFE", "exchange": "NYSE", "country": "US", "cap": "large"},
    "moderna": {"ticker": "MRNA", "exchange": "Nasdaq", "country": "US", "cap": "mid"},
    "micron": {"ticker": "MU", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "pinterest": {"ticker": "PINS", "exchange": "NYSE", "country": "US", "cap": "mid"},
    "conocophillips": {"ticker": "COP", "exchange": "NYSE", "country": "US", "cap": "large"},
    "conoco": {"ticker": "COP", "exchange": "NYSE", "country": "US", "cap": "large"},
    "openai": {"ticker": "N/A private", "exchange": "N/A", "country": "US", "cap": "private"},
    "spacex": {"ticker": "N/A private", "exchange": "N/A", "country": "US", "cap": "private"},
    "stripe": {"ticker": "N/A private", "exchange": "N/A", "country": "US", "cap": "private"},
    "huawei": {"ticker": "N/A private", "exchange": "N/A", "country": "CN", "cap": "private"},
    "msc": {"ticker": "N/A private", "exchange": "N/A", "country": "CH", "cap": "private"},
    "mediterranean shipping": {"ticker": "N/A private", "exchange": "N/A", "country": "CH", "cap": "private"},
    # --- Europe ---
    "asml": {"ticker": "ASML", "exchange": "Euronext Amsterdam", "country": "NL", "cap": "mega"},
    "lvmh": {"ticker": "MC", "exchange": "Euronext Paris", "country": "FR", "cap": "mega"},
    "siemens": {"ticker": "SIE", "exchange": "Xetra", "country": "DE", "cap": "mega"},
    "volkswagen": {"ticker": "VOW3", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "bmw": {"ticker": "BMW", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "mercedes-benz": {"ticker": "MBG", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "deutsche bank": {"ticker": "DBK", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "ing groep": {"ticker": "INGA", "exchange": "Euronext Amsterdam", "country": "NL", "cap": "large"},
    "swedbank": {"ticker": "SWED-A", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "seb": {"ticker": "SEB-A", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "nordea": {"ticker": "NDA-FI", "exchange": "Nasdaq Helsinki", "country": "FI", "cap": "large"},
    # --- Asia: Korea ---
    "samsung": {"ticker": "005930", "provider_symbol": "005930.KS", "exchange": "KRX", "country": "KR", "cap": "mega"},
    "samsung electronics": {"ticker": "005930", "provider_symbol": "005930.KS", "exchange": "KRX", "country": "KR", "cap": "mega"},
    "sk hynix": {"ticker": "000660", "provider_symbol": "000660.KS", "exchange": "KRX", "country": "KR", "cap": "mega"},
    "hynix": {"ticker": "000660", "provider_symbol": "000660.KS", "exchange": "KRX", "country": "KR", "cap": "mega"},
    "lg energy solution": {"ticker": "373220", "provider_symbol": "373220.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    "lg chem": {"ticker": "051910", "provider_symbol": "051910.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    "samsung sdi": {"ticker": "006400", "provider_symbol": "006400.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    "hyundai motor": {"ticker": "005380", "provider_symbol": "005380.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    "hyundai": {"ticker": "005380", "provider_symbol": "005380.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    "kia corp": {"ticker": "000270", "provider_symbol": "000270.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    "kia": {"ticker": "000270", "provider_symbol": "000270.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    "naver": {"ticker": "035420", "provider_symbol": "035420.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    "kakao": {"ticker": "035720", "provider_symbol": "035720.KS", "exchange": "KRX", "country": "KR", "cap": "large"},
    # --- Asia: Taiwan ---
    "tsmc": {"ticker": "2330", "provider_symbol": "2330.TW", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "mega", "adr_ticker": "TSM", "adr_exchange": "NYSE"},
    "taiwan semiconductor": {"ticker": "2330", "provider_symbol": "2330.TW", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "mega", "adr_ticker": "TSM", "adr_exchange": "NYSE"},
    "taiwan semiconductor manufacturing": {"ticker": "2330", "provider_symbol": "2330.TW", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "mega", "adr_ticker": "TSM", "adr_exchange": "NYSE"},
    "hon hai": {"ticker": "2317", "provider_symbol": "2317.TW", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "large"},
    "foxconn": {"ticker": "2317", "provider_symbol": "2317.TW", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "large"},
    "mediatek": {"ticker": "2454", "provider_symbol": "2454.TW", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "large"},
    "united microelectronics": {"ticker": "2303", "provider_symbol": "2303.TW", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "large", "adr_ticker": "UMC", "adr_exchange": "NYSE"},
    "umc": {"ticker": "2303", "provider_symbol": "2303.TW", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "large", "adr_ticker": "UMC", "adr_exchange": "NYSE"},
    # --- Asia: Japan ---
    "toyota": {"ticker": "7203", "provider_symbol": "7203.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "mega", "adr_ticker": "TM", "adr_exchange": "NYSE"},
    "toyota motor": {"ticker": "7203", "provider_symbol": "7203.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "mega", "adr_ticker": "TM", "adr_exchange": "NYSE"},
    "honda": {"ticker": "7267", "provider_symbol": "7267.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large", "adr_ticker": "HMC", "adr_exchange": "NYSE"},
    "honda motor": {"ticker": "7267", "provider_symbol": "7267.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large", "adr_ticker": "HMC", "adr_exchange": "NYSE"},
    "nissan": {"ticker": "7201", "provider_symbol": "7201.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "sony": {"ticker": "6758", "provider_symbol": "6758.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "mega", "adr_ticker": "SONY", "adr_exchange": "NYSE"},
    "sony group": {"ticker": "6758", "provider_symbol": "6758.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "mega", "adr_ticker": "SONY", "adr_exchange": "NYSE"},
    "softbank group": {"ticker": "9984", "provider_symbol": "9984.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "softbank": {"ticker": "9984", "provider_symbol": "9984.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "nintendo": {"ticker": "7974", "provider_symbol": "7974.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "hitachi": {"ticker": "6501", "provider_symbol": "6501.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "tokyo electron": {"ticker": "8035", "provider_symbol": "8035.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "advantest": {"ticker": "6857", "provider_symbol": "6857.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "renesas": {"ticker": "6723", "provider_symbol": "6723.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "renesas electronics": {"ticker": "6723", "provider_symbol": "6723.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "mitsubishi ufj": {"ticker": "8306", "provider_symbol": "8306.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "mega", "adr_ticker": "MUFG", "adr_exchange": "NYSE"},
    "sumitomo mitsui": {"ticker": "8316", "provider_symbol": "8316.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large", "adr_ticker": "SMFG", "adr_exchange": "NYSE"},
    "mizuho": {"ticker": "8411", "provider_symbol": "8411.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large", "adr_ticker": "MFG", "adr_exchange": "NYSE"},
    "mitsubishi corp": {"ticker": "8058", "provider_symbol": "8058.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "mitsui": {"ticker": "8031", "provider_symbol": "8031.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "fast retailing": {"ticker": "9983", "provider_symbol": "9983.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    "uniqlo": {"ticker": "9983", "provider_symbol": "9983.T", "exchange": "Tokyo Stock Exchange", "country": "JP", "cap": "large"},
    # --- Asia: China / Hong Kong ---
    "tencent": {"ticker": "0700", "provider_symbol": "0700.HK", "exchange": "HKEX", "country": "CN", "cap": "mega"},
    "alibaba": {"ticker": "9988", "provider_symbol": "9988.HK", "exchange": "HKEX", "country": "CN", "cap": "mega", "adr_ticker": "BABA", "adr_exchange": "NYSE"},
    "byd": {"ticker": "1211", "provider_symbol": "1211.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "byd co": {"ticker": "1211", "provider_symbol": "1211.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "byd company": {"ticker": "1211", "provider_symbol": "1211.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "zte": {"ticker": "0763", "provider_symbol": "0763.HK", "exchange": "HKEX", "country": "CN", "cap": "mid"},
    "xiaomi": {"ticker": "1810", "provider_symbol": "1810.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "meituan": {"ticker": "3690", "provider_symbol": "3690.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "baidu": {"ticker": "9888", "provider_symbol": "9888.HK", "exchange": "HKEX", "country": "CN", "cap": "large", "adr_ticker": "BIDU", "adr_exchange": "Nasdaq"},
    "kuaishou": {"ticker": "1024", "provider_symbol": "1024.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "smic": {"ticker": "0981", "provider_symbol": "0981.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "semiconductor manufacturing international": {"ticker": "0981", "provider_symbol": "0981.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "catl": {"ticker": "300750", "provider_symbol": "300750.SZ", "exchange": "Shenzhen Stock Exchange", "country": "CN", "cap": "large"},
    "contemporary amperex": {"ticker": "300750", "provider_symbol": "300750.SZ", "exchange": "Shenzhen Stock Exchange", "country": "CN", "cap": "large"},
    "ping an": {"ticker": "2318", "provider_symbol": "2318.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "petrochina": {"ticker": "0857", "provider_symbol": "0857.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "cnooc": {"ticker": "0883", "provider_symbol": "0883.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "china mobile": {"ticker": "0941", "provider_symbol": "0941.HK", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "li auto": {"ticker": "2015", "provider_symbol": "2015.HK", "exchange": "HKEX", "country": "CN", "cap": "large", "adr_ticker": "LI", "adr_exchange": "Nasdaq"},
    "nio": {"ticker": "9866", "provider_symbol": "9866.HK", "exchange": "HKEX", "country": "CN", "cap": "mid", "adr_ticker": "NIO", "adr_exchange": "NYSE"},
    "xpeng": {"ticker": "9868", "provider_symbol": "9868.HK", "exchange": "HKEX", "country": "CN", "cap": "mid", "adr_ticker": "XPEV", "adr_exchange": "NYSE"},
    # --- Asia: India ---
    "reliance industries": {"ticker": "RELIANCE", "provider_symbol": "RELIANCE.NS", "exchange": "NSE India", "country": "IN", "cap": "mega"},
    "tata motors": {"ticker": "TATAMOTORS", "provider_symbol": "TATAMOTORS.NS", "exchange": "NSE India", "country": "IN", "cap": "large"},
    "tata consultancy": {"ticker": "TCS", "provider_symbol": "TCS.NS", "exchange": "NSE India", "country": "IN", "cap": "mega"},
    "infosys": {"ticker": "INFY", "provider_symbol": "INFY.NS", "exchange": "NSE India", "country": "IN", "cap": "large", "adr_ticker": "INFY", "adr_exchange": "NYSE"},
    "hdfc bank": {"ticker": "HDFCBANK", "provider_symbol": "HDFCBANK.NS", "exchange": "NSE India", "country": "IN", "cap": "large", "adr_ticker": "HDB", "adr_exchange": "NYSE"},
    "icici bank": {"ticker": "ICICIBANK", "provider_symbol": "ICICIBANK.NS", "exchange": "NSE India", "country": "IN", "cap": "large", "adr_ticker": "IBN", "adr_exchange": "NYSE"},
    "adani enterprises": {"ticker": "ADANIENT", "provider_symbol": "ADANIENT.NS", "exchange": "NSE India", "country": "IN", "cap": "large"},
    "mahindra mahindra": {"ticker": "M&M", "provider_symbol": "M&M.NS", "exchange": "NSE India", "country": "IN", "cap": "large"},
    # --- Nasdaq Vilnius (additional LT listed) ---
    "akola": {"ticker": "AKO1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "linas agro": {"ticker": "AKO1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "amber grid": {"ticker": "AMG1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "invalda": {"ticker": "IVL1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "mid"},
    "invl technology": {"ticker": "INC1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "invl baltic real estate": {"ticker": "INR1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "invl baltic farmland": {"ticker": "INL1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "kauno energija": {"ticker": "KNR1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "novaturas": {"ticker": "NTU1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "žemaitijos pienas": {"ticker": "ZMP1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "zemaitijos pienas": {"ticker": "ZMP1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "utenos trikotažas": {"ticker": "UTR1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "utenos trikotazas": {"ticker": "UTR1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "micro"},
    "vilniaus baldai": {"ticker": "VBL1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "east west agro": {"ticker": "EWA1L", "exchange": "Nasdaq Vilnius", "country": "LT", "cap": "small"},
    "neo finance": {"ticker": "NEOFI", "exchange": "Nasdaq Vilnius First North", "country": "LT", "cap": "small"},
    "k2 lt": {"ticker": "K2LT", "exchange": "Nasdaq Vilnius First North", "country": "LT", "cap": "small"},
    # --- Nasdaq Tallinn (additional EE listed) ---
    "infortar": {"ticker": "INF1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "large"},
    "eften": {"ticker": "EFT1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "mid"},
    "arco vara": {"ticker": "ARC1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "micro"},
    "hepsor": {"ticker": "HPR1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "micro"},
    "nordecon": {"ticker": "NCN1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "micro"},
    "pro kapital": {"ticker": "PKG1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "ekspress grupp": {"ticker": "EEG1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "silvano": {"ticker": "SFG1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "prfoods": {"ticker": "PRF1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "micro"},
    "nordic fibreboard": {"ticker": "SKN1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "micro"},
    "trigon property": {"ticker": "TPD1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "micro"},
    "textmagic": {"ticker": "MAGIC", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "small"},
    "airobot": {"ticker": "AIR", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "micro"},
    "modera": {"ticker": "MODE", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "small"},
    "saunum": {"ticker": "SAUNA", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "small"},
    # --- Nasdaq Riga (additional LV listed) ---
    "delfingroup": {"ticker": "DGR1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "small"},
    "eleving": {"ticker": "ELEVR", "exchange": "Nasdaq Riga", "country": "LV", "cap": "mid"},
    "indexo": {"ticker": "IDX1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "small"},
    "saf tehnika": {"ticker": "SAF1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "small"},
    "amber latvijas balzams": {"ticker": "BAL1R", "exchange": "Nasdaq Riga", "country": "LV", "cap": "micro"},
    "virši": {"ticker": "VIRSI", "exchange": "Nasdaq Riga First North", "country": "LV", "cap": "small"},
    "virsi": {"ticker": "VIRSI", "exchange": "Nasdaq Riga First North", "country": "LV", "cap": "small"},
    "madara": {"ticker": "MDARA", "exchange": "Nasdaq Riga First North", "country": "LV", "cap": "small"},
    "kalve coffee": {"ticker": "KALVE", "exchange": "Nasdaq Riga First North", "country": "LV", "cap": "small"},
    # --- Europe: banks / finance / fintech ---
    "lloyds": {"ticker": "LLOY", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    "barclays": {"ticker": "BARC", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    "hsbc": {"ticker": "HSBA", "exchange": "London Stock Exchange", "country": "GB", "cap": "mega"},
    "danske bank": {"ticker": "DANSKE", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "large"},
    "dnb": {"ticker": "DNB", "exchange": "Oslo Børs", "country": "NO", "cap": "large"},
    "wise": {"ticker": "WISE", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    # --- Europe: energy / utilities ---
    "orlen": {"ticker": "PKN", "exchange": "Warsaw Stock Exchange", "country": "PL", "cap": "large"},
    "equinor": {"ticker": "EQNR", "exchange": "Oslo Børs", "country": "NO", "cap": "mega"},
    "neste": {"ticker": "NESTE", "exchange": "Nasdaq Helsinki", "country": "FI", "cap": "large"},
    "fortum": {"ticker": "FORTUM", "exchange": "Nasdaq Helsinki", "country": "FI", "cap": "large"},
    "orsted": {"ticker": "ORSTED", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "large"},
    "ørsted": {"ticker": "ORSTED", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "large"},
    "vestas": {"ticker": "VWS", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "large"},
    "rwe": {"ticker": "RWE", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "e.on": {"ticker": "EOAN", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "shell": {"ticker": "SHEL", "exchange": "London Stock Exchange", "country": "GB", "cap": "mega"},
    "bp": {"ticker": "BP.", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    "totalenergies": {"ticker": "TTE", "exchange": "Euronext Paris", "country": "FR", "cap": "mega"},
    # --- Europe: logistics / transport ---
    "maersk": {"ticker": "MAERSK-B", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "large"},
    "møller mærsk": {"ticker": "MAERSK-B", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "large"},
    "moller maersk": {"ticker": "MAERSK-B", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "large"},
    "hapag-lloyd": {"ticker": "HLAG", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "dsv": {"ticker": "DSV", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "large"},
    "dhl group": {"ticker": "DHL", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "kuehne": {"ticker": "KNIN", "exchange": "SIX Swiss Exchange", "country": "CH", "cap": "large"},
    "ryanair": {"ticker": "RYA", "exchange": "Euronext Dublin", "country": "IE", "cap": "large"},
    "wizz air": {"ticker": "WIZZ", "exchange": "London Stock Exchange", "country": "GB", "cap": "mid"},
    # --- Europe: defense / industrials ---
    "rheinmetall": {"ticker": "RHM", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "bae systems": {"ticker": "BA.", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    "saab": {"ticker": "SAAB-B", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "leonardo": {"ticker": "LDO", "exchange": "Borsa Italiana", "country": "IT", "cap": "large"},
    "thales": {"ticker": "HO", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "kongsberg": {"ticker": "KOG", "exchange": "Oslo Børs", "country": "NO", "cap": "large"},
    "airbus": {"ticker": "AIR", "exchange": "Euronext Paris", "country": "NL", "cap": "mega"},
    # --- Europe: telcos / tech ---
    "telia company": {"ticker": "TELIA", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "tele2": {"ticker": "TEL2-B", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "ericsson": {"ticker": "ERIC-B", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "nokia": {"ticker": "NOKIA", "exchange": "Nasdaq Helsinki", "country": "FI", "cap": "large"},
    "deutsche telekom": {"ticker": "DTE", "exchange": "Xetra", "country": "DE", "cap": "mega"},
    "orange": {"ticker": "ORA", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    # --- Global: semiconductors / AI infrastructure ---
    "broadcom": {"ticker": "AVGO", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "qualcomm": {"ticker": "QCOM", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "arm holdings": {"ticker": "ARM", "exchange": "Nasdaq", "country": "GB", "cap": "large"},
    "super micro": {"ticker": "SMCI", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "applied materials": {"ticker": "AMAT", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "lam research": {"ticker": "LRCX", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    # --- Global: commodities / agriculture ---
    "glencore": {"ticker": "GLEN", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    "bunge": {"ticker": "BG", "exchange": "NYSE", "country": "US", "cap": "large"},
    "archer-daniels": {"ticker": "ADM", "exchange": "NYSE", "country": "US", "cap": "large"},
    "freeport": {"ticker": "FCX", "exchange": "NYSE", "country": "US", "cap": "large"},
    "rio tinto": {"ticker": "RIO", "exchange": "London Stock Exchange", "country": "GB", "cap": "mega"},
    # --- US: defense ---
    "northrop": {"ticker": "NOC", "exchange": "NYSE", "country": "US", "cap": "large"},
    "general dynamics": {"ticker": "GD", "exchange": "NYSE", "country": "US", "cap": "large"},
    "l3harris": {"ticker": "LHX", "exchange": "NYSE", "country": "US", "cap": "large"},
    "huntington ingalls": {"ticker": "HII", "exchange": "NYSE", "country": "US", "cap": "mid"},
    # --- US: retail / consumer ---
    "walmart": {"ticker": "WMT", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "costco": {"ticker": "COST", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "target": {"ticker": "TGT", "exchange": "NYSE", "country": "US", "cap": "large"},
    "home depot": {"ticker": "HD", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "pdd": {"ticker": "PDD", "exchange": "Nasdaq", "country": "CN", "cap": "large"},
    "jd.com": {"ticker": "9618", "provider_symbol": "9618.HK", "exchange": "HKEX", "country": "CN", "cap": "large", "adr_ticker": "JD", "adr_exchange": "Nasdaq"},
    "jd com": {"ticker": "9618", "provider_symbol": "9618.HK", "exchange": "HKEX", "country": "CN", "cap": "large", "adr_ticker": "JD", "adr_exchange": "Nasdaq"},
    "mercadolibre": {"ticker": "MELI", "exchange": "Nasdaq", "country": "UY", "cap": "large"},
    # --- Global: autos ---
    "stellantis": {"ticker": "STLAM", "exchange": "Borsa Italiana", "country": "NL", "cap": "large"},
    "renault": {"ticker": "RNO", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "volvo": {"ticker": "VOLV-B", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    # --- Baltic / regional private & state-owned ---
    "elektrum lietuva": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "latvenergo": {"ticker": "N/A private", "exchange": "N/A", "country": "LV", "cap": "state-owned"},
    "enefit": {"ticker": "N/A private", "exchange": "N/A", "country": "EE", "cap": "state-owned"},
    "eesti energia": {"ticker": "N/A private", "exchange": "N/A", "country": "EE", "cap": "state-owned"},
    "luminor": {"ticker": "N/A private", "exchange": "N/A", "country": "EE", "cap": "private"},
    "curve europe": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "mantinga": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "civinity": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "detonas": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "state-owned"},
    "ltg": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "state-owned"},
    "lietuvos geležinkeliai": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "state-owned"},
    "lietuvos gelezinkeliai": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "state-owned"},
    # --- Global private ---
    "cma cgm": {"ticker": "N/A private", "exchange": "N/A", "country": "FR", "cap": "private"},
    "dp world": {"ticker": "N/A private", "exchange": "N/A", "country": "AE", "cap": "state-owned"},
    "vitol": {"ticker": "N/A private", "exchange": "N/A", "country": "CH", "cap": "private"},
    "trafigura": {"ticker": "N/A private", "exchange": "N/A", "country": "SG", "cap": "private"},
    "gunvor": {"ticker": "N/A private", "exchange": "N/A", "country": "CH", "cap": "private"},
    "cargill": {"ticker": "N/A private", "exchange": "N/A", "country": "US", "cap": "private"},
    "anthropic": {"ticker": "N/A private", "exchange": "N/A", "country": "US", "cap": "private"},
    "anduril": {"ticker": "N/A private", "exchange": "N/A", "country": "US", "cap": "private"},
    "databricks": {"ticker": "N/A private", "exchange": "N/A", "country": "US", "cap": "private"},
    "revolut": {"ticker": "N/A private", "exchange": "N/A", "country": "GB", "cap": "private"},
    "klarna": {"ticker": "N/A private", "exchange": "N/A", "country": "SE", "cap": "private"},
    # --- Baltic First North / smaller names ---
    "liven": {"ticker": "LVN1T", "exchange": "Nasdaq Tallinn", "country": "EE", "cap": "small"},
    "agrova baltics": {"ticker": "EGG", "exchange": "Nasdaq Riga First North", "country": "LV", "cap": "small"},
    "bercman technologies": {"ticker": "BERCM", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "micro"},
    "punktid technologies": {"ticker": "PNKTD", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "micro"},
    "primostar group": {"ticker": "PRIMO", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "small"},
    "j molner": {"ticker": "MOLNR", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "small"},
    "j. molner": {"ticker": "MOLNR", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "small"},
    "robus group": {"ticker": "ROBUS", "exchange": "Nasdaq Tallinn First North", "country": "EE", "cap": "micro"},
    "city service": {"ticker": "CTS1L", "exchange": "Nasdaq Vilnius First North", "country": "LT", "cap": "micro"},
    # --- LT/Baltic subsidiary or private (must NOT enter section C even if parent is listed) ---
    "orlen lietuva": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "subsidiary-private"},
    "maxima grupė": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "maxima grupe": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "vilniaus prekyba": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "sanitex": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "rimi lietuva": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "lidl lietuva": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "circle k lietuva": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "subsidiary-private"},
    "circle k": {"ticker": "N/A private", "exchange": "N/A", "country": "US", "cap": "subsidiary-private"},
    "revolut bank": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "vėjomaina": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "vejomaina": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "realco": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "citus": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "eika": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    "omberg group": {"ticker": "N/A private", "exchange": "N/A", "country": "LT", "cap": "private"},
    # --- Europe: banks / finance ---
    "bnp paribas": {"ticker": "BNP", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "société générale": {"ticker": "GLE", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "societe generale": {"ticker": "GLE", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "santander": {"ticker": "SAN", "exchange": "BME Madrid", "country": "ES", "cap": "large"},
    "unicredit": {"ticker": "UCG", "exchange": "Borsa Italiana", "country": "IT", "cap": "large"},
    "intesa sanpaolo": {"ticker": "ISP", "exchange": "Borsa Italiana", "country": "IT", "cap": "large"},
    "ubs group": {"ticker": "UBSG", "exchange": "SIX Swiss Exchange", "country": "CH", "cap": "large"},
    "commerzbank": {"ticker": "CBK", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "erste group": {"ticker": "EBS", "exchange": "Vienna Stock Exchange", "country": "AT", "cap": "large"},
    "raiffeisen bank": {"ticker": "RBI", "exchange": "Vienna Stock Exchange", "country": "AT", "cap": "large"},
    "deutsche boerse": {"ticker": "DB1", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "deutsche börse": {"ticker": "DB1", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "london stock exchange group": {"ticker": "LSEG", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    # --- Europe: energy / utilities / infrastructure ---
    "iberdrola": {"ticker": "IBE", "exchange": "BME Madrid", "country": "ES", "cap": "mega"},
    "enel": {"ticker": "ENEL", "exchange": "Borsa Italiana", "country": "IT", "cap": "large"},
    "engie": {"ticker": "ENGI", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "veolia": {"ticker": "VIE", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "siemens energy": {"ticker": "ENR", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "schneider electric": {"ticker": "SU", "exchange": "Euronext Paris", "country": "FR", "cap": "mega"},
    "abb": {"ticker": "ABBN", "exchange": "SIX Swiss Exchange", "country": "CH", "cap": "large"},
    "kone oyj": {"ticker": "KNEBV", "exchange": "Nasdaq Helsinki", "country": "FI", "cap": "large"},
    "kone": {"ticker": "KNEBV", "exchange": "Nasdaq Helsinki", "country": "FI", "cap": "large"},
    "sdiptech": {"ticker": "SDIP B", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "mid"},
    "tk elevator": {"ticker": "N/A private", "exchange": "N/A", "country": "DE", "cap": "private"},
    # --- Europe: defense / aerospace ---
    "hensoldt": {"ticker": "HAG", "exchange": "Xetra", "country": "DE", "cap": "mid"},
    "dassault aviation": {"ticker": "AM", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "rolls-royce": {"ticker": "RR.", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    "mtu aero": {"ticker": "MTX", "exchange": "Xetra", "country": "DE", "cap": "large"},
    # --- Europe: retail / consumer / luxury ---
    "tesco": {"ticker": "TSCO", "exchange": "London Stock Exchange", "country": "GB", "cap": "large"},
    "carrefour": {"ticker": "CA", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "inditex": {"ticker": "ITX", "exchange": "BME Madrid", "country": "ES", "cap": "mega"},
    "hennes mauritz": {"ticker": "HM B", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "h&m": {"ticker": "HM B", "exchange": "Nasdaq Stockholm", "country": "SE", "cap": "large"},
    "hermès": {"ticker": "RMS", "exchange": "Euronext Paris", "country": "FR", "cap": "mega"},
    "hermes": {"ticker": "RMS", "exchange": "Euronext Paris", "country": "FR", "cap": "mega"},
    "kering": {"ticker": "KER", "exchange": "Euronext Paris", "country": "FR", "cap": "large"},
    "richemont": {"ticker": "CFR", "exchange": "SIX Swiss Exchange", "country": "CH", "cap": "large"},
    "adidas": {"ticker": "ADS", "exchange": "Xetra", "country": "DE", "cap": "large"},
    "puma": {"ticker": "PUM", "exchange": "Xetra", "country": "DE", "cap": "mid"},
    "l'oréal": {"ticker": "OR", "exchange": "Euronext Paris", "country": "FR", "cap": "mega"},
    "loreal": {"ticker": "OR", "exchange": "Euronext Paris", "country": "FR", "cap": "mega"},
    "aldi sud": {"ticker": "N/A private", "exchange": "N/A", "country": "DE", "cap": "private"},
    "aldi süd": {"ticker": "N/A private", "exchange": "N/A", "country": "DE", "cap": "private"},
    "schwarz gruppe": {"ticker": "N/A private", "exchange": "N/A", "country": "DE", "cap": "private"},
    "rewe group": {"ticker": "N/A private", "exchange": "N/A", "country": "DE", "cap": "private"},
    # --- Global: shipping ---
    "cosco shipping": {"ticker": "1919", "exchange": "HKEX", "country": "CN", "cap": "large"},
    "evergreen marine": {"ticker": "2603", "exchange": "Taiwan Stock Exchange", "country": "TW", "cap": "large"},
    "zim integrated shipping": {"ticker": "ZIM", "exchange": "NYSE", "country": "IL", "cap": "mid"},
    "frontline": {"ticker": "FRO", "exchange": "NYSE", "country": "CY", "cap": "mid"},
    "hafnia": {"ticker": "HAFNI", "exchange": "Oslo Børs", "country": "SG", "cap": "mid"},
    "torm": {"ticker": "TRMD A", "exchange": "Nasdaq Copenhagen", "country": "GB", "cap": "mid"},
    # --- Global: AI / software ---
    "oracle": {"ticker": "ORCL", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "salesforce": {"ticker": "CRM", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "snowflake": {"ticker": "SNOW", "exchange": "NYSE", "country": "US", "cap": "large"},
    "servicenow": {"ticker": "NOW", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "synopsys": {"ticker": "SNPS", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "cadence design": {"ticker": "CDNS", "exchange": "Nasdaq", "country": "US", "cap": "large"},
    "adobe": {"ticker": "ADBE", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "sap": {"ticker": "SAP", "exchange": "Xetra", "country": "DE", "cap": "mega"},
    "shopify": {"ticker": "SHOP", "exchange": "NYSE", "country": "CA", "cap": "large"},
    # --- Global: payments / finance ---
    "visa": {"ticker": "V", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "mastercard": {"ticker": "MA", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "blackrock": {"ticker": "BLK", "exchange": "NYSE", "country": "US", "cap": "large"},
    "blackstone": {"ticker": "BX", "exchange": "NYSE", "country": "US", "cap": "large"},
    "brookfield": {"ticker": "BN", "exchange": "NYSE", "country": "CA", "cap": "large"},
    "alimentation couche-tard": {"ticker": "ATD", "exchange": "Toronto Stock Exchange", "country": "CA", "cap": "large"},
    # --- Global: consumer / defensive ---
    "nestle": {"ticker": "NESN", "exchange": "SIX Swiss Exchange", "country": "CH", "cap": "mega"},
    "nestlé": {"ticker": "NESN", "exchange": "SIX Swiss Exchange", "country": "CH", "cap": "mega"},
    "unilever": {"ticker": "ULVR", "exchange": "London Stock Exchange", "country": "GB", "cap": "mega"},
    "procter gamble": {"ticker": "PG", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "pepsico": {"ticker": "PEP", "exchange": "Nasdaq", "country": "US", "cap": "mega"},
    "mcdonalds": {"ticker": "MCD", "exchange": "NYSE", "country": "US", "cap": "mega"},
    # --- Global: pharma / healthcare ---
    "novo nordisk": {"ticker": "NOVO B", "exchange": "Nasdaq Copenhagen", "country": "DK", "cap": "mega"},
    "novartis": {"ticker": "NOVN", "exchange": "SIX Swiss Exchange", "country": "CH", "cap": "mega"},
    "roche": {"ticker": "ROG", "exchange": "SIX Swiss Exchange", "country": "CH", "cap": "mega"},
    "astrazeneca": {"ticker": "AZN", "exchange": "London Stock Exchange", "country": "GB", "cap": "mega"},
    "eli lilly": {"ticker": "LLY", "exchange": "NYSE", "country": "US", "cap": "mega"},
    "johnson johnson": {"ticker": "JNJ", "exchange": "NYSE", "country": "US", "cap": "mega"},
}

# Macro instruments: indices, commodities, crypto, FX, rates/bonds, ETF proxies.
# These NEVER appear in Direct Public Tickers (section C); they go to Market Instruments.
# Keys: lowercase normalized names and common aliases.
INSTRUMENT_MAP: dict[str, dict] = {
    # --- Equity Indices: US ---
    "s&p 500": {"symbol": "SPX", "display": "S&P 500", "asset_class": "equity_index", "region": "US", "provider_symbol": "^GSPC"},
    "sp500": {"symbol": "SPX", "display": "S&P 500", "asset_class": "equity_index", "region": "US", "provider_symbol": "^GSPC"},
    "spx": {"symbol": "SPX", "display": "S&P 500", "asset_class": "equity_index", "region": "US", "provider_symbol": "^GSPC"},
    "s&p500": {"symbol": "SPX", "display": "S&P 500", "asset_class": "equity_index", "region": "US", "provider_symbol": "^GSPC"},
    "nasdaq 100": {"symbol": "NDX", "display": "Nasdaq 100", "asset_class": "equity_index", "region": "US", "provider_symbol": "^NDX"},
    "nasdaq100": {"symbol": "NDX", "display": "Nasdaq 100", "asset_class": "equity_index", "region": "US", "provider_symbol": "^NDX"},
    "ndx": {"symbol": "NDX", "display": "Nasdaq 100", "asset_class": "equity_index", "region": "US", "provider_symbol": "^NDX"},
    "qqq": {"symbol": "NDX", "display": "Nasdaq 100 (QQQ ETF)", "asset_class": "equity_index", "region": "US", "provider_symbol": "QQQ"},
    "dow jones": {"symbol": "DJIA", "display": "Dow Jones", "asset_class": "equity_index", "region": "US", "provider_symbol": "^DJI"},
    "djia": {"symbol": "DJIA", "display": "Dow Jones", "asset_class": "equity_index", "region": "US", "provider_symbol": "^DJI"},
    "russell 2000": {"symbol": "RUT", "display": "Russell 2000", "asset_class": "equity_index", "region": "US", "provider_symbol": "^RUT"},
    "vix": {"symbol": "VIX", "display": "VIX (Fear Index)", "asset_class": "volatility_index", "region": "US", "provider_symbol": "^VIX"},
    # --- Equity Indices: Europe ---
    "euro stoxx 50": {"symbol": "SX5E", "display": "Euro Stoxx 50", "asset_class": "equity_index", "region": "EU", "provider_symbol": "^STOXX50E"},
    "stoxx 50": {"symbol": "SX5E", "display": "Euro Stoxx 50", "asset_class": "equity_index", "region": "EU", "provider_symbol": "^STOXX50E"},
    "dax": {"symbol": "DAX", "display": "DAX (Germany)", "asset_class": "equity_index", "region": "DE", "provider_symbol": "^GDAXI"},
    "ftse 100": {"symbol": "UKX", "display": "FTSE 100 (UK)", "asset_class": "equity_index", "region": "GB", "provider_symbol": "^FTSE"},
    "cac 40": {"symbol": "CAC", "display": "CAC 40 (France)", "asset_class": "equity_index", "region": "FR", "provider_symbol": "^FCHI"},
    "cac40": {"symbol": "CAC", "display": "CAC 40 (France)", "asset_class": "equity_index", "region": "FR", "provider_symbol": "^FCHI"},
    # --- Equity Indices: Baltic ---
    "omx baltic": {"symbol": "OMXB", "display": "OMX Baltic GI", "asset_class": "equity_index", "region": "Baltic", "provider_symbol": "OMXBGI"},
    "omx vilnius": {"symbol": "OMXV", "display": "OMX Vilnius GI", "asset_class": "equity_index", "region": "LT", "provider_symbol": "OMXVGI"},
    "omx riga": {"symbol": "OMXR", "display": "OMX Riga GI", "asset_class": "equity_index", "region": "LV", "provider_symbol": "OMXRGI"},
    "omx tallinn": {"symbol": "OMXT", "display": "OMX Tallinn GI", "asset_class": "equity_index", "region": "EE", "provider_symbol": "OMXTGI"},
    # --- Commodities: Energy ---
    "brent": {"symbol": "BRNT", "display": "Brent Crude Oil", "asset_class": "commodity", "region": "global", "provider_symbol": "BZ=F", "unit": "USD/bbl"},
    "brent crude": {"symbol": "BRNT", "display": "Brent Crude Oil", "asset_class": "commodity", "region": "global", "provider_symbol": "BZ=F", "unit": "USD/bbl"},
    "wti": {"symbol": "WTI", "display": "WTI Crude Oil", "asset_class": "commodity", "region": "global", "provider_symbol": "CL=F", "unit": "USD/bbl"},
    "wti crude": {"symbol": "WTI", "display": "WTI Crude Oil", "asset_class": "commodity", "region": "global", "provider_symbol": "CL=F", "unit": "USD/bbl"},
    "crude oil": {"symbol": "WTI", "display": "Crude Oil (WTI)", "asset_class": "commodity", "region": "global", "provider_symbol": "CL=F", "unit": "USD/bbl"},
    "natural gas": {"symbol": "NATGAS", "display": "Natural Gas (Henry Hub)", "asset_class": "commodity", "region": "global", "provider_symbol": "NG=F", "unit": "USD/MMBtu"},
    "ttf gas": {"symbol": "TTF", "display": "TTF Natural Gas (Europe)", "asset_class": "commodity", "region": "EU", "provider_symbol": "TTF", "unit": "EUR/MWh"},
    "ttf": {"symbol": "TTF", "display": "TTF Natural Gas (Europe)", "asset_class": "commodity", "region": "EU", "provider_symbol": "TTF", "unit": "EUR/MWh"},
    # --- Commodities: Metals ---
    "gold": {"symbol": "GOLD", "display": "Gold", "asset_class": "commodity", "region": "global", "provider_symbol": "GC=F", "unit": "USD/oz"},
    "silver": {"symbol": "SILVER", "display": "Silver", "asset_class": "commodity", "region": "global", "provider_symbol": "SI=F", "unit": "USD/oz"},
    "copper": {"symbol": "COPPER", "display": "Copper", "asset_class": "commodity", "region": "global", "provider_symbol": "HG=F", "unit": "USD/lb"},
    # --- Commodities: Agriculture ---
    "wheat": {"symbol": "WHEAT", "display": "Wheat (CBOT)", "asset_class": "commodity", "region": "global", "provider_symbol": "ZW=F", "unit": "USD/bu"},
    "corn": {"symbol": "CORN", "display": "Corn (CBOT)", "asset_class": "commodity", "region": "global", "provider_symbol": "ZC=F", "unit": "USD/bu"},
    "soybeans": {"symbol": "SOYB", "display": "Soybeans (CBOT)", "asset_class": "commodity", "region": "global", "provider_symbol": "ZS=F", "unit": "USD/bu"},
    # --- Crypto ---
    "bitcoin": {"symbol": "BTC", "display": "Bitcoin", "asset_class": "crypto", "region": "global", "provider_symbol": "BTC-USD"},
    "btc": {"symbol": "BTC", "display": "Bitcoin", "asset_class": "crypto", "region": "global", "provider_symbol": "BTC-USD"},
    "ethereum": {"symbol": "ETH", "display": "Ethereum", "asset_class": "crypto", "region": "global", "provider_symbol": "ETH-USD"},
    "eth": {"symbol": "ETH", "display": "Ethereum", "asset_class": "crypto", "region": "global", "provider_symbol": "ETH-USD"},
    # --- FX ---
    "eur/usd": {"symbol": "EURUSD", "display": "EUR/USD", "asset_class": "fx", "region": "global", "provider_symbol": "EURUSD=X"},
    "eurusd": {"symbol": "EURUSD", "display": "EUR/USD", "asset_class": "fx", "region": "global", "provider_symbol": "EURUSD=X"},
    "usd/eur": {"symbol": "EURUSD", "display": "EUR/USD", "asset_class": "fx", "region": "global", "provider_symbol": "EURUSD=X"},
    "usd/jpy": {"symbol": "USDJPY", "display": "USD/JPY", "asset_class": "fx", "region": "global", "provider_symbol": "JPY=X"},
    "usdjpy": {"symbol": "USDJPY", "display": "USD/JPY", "asset_class": "fx", "region": "global", "provider_symbol": "JPY=X"},
    "gbp/usd": {"symbol": "GBPUSD", "display": "GBP/USD", "asset_class": "fx", "region": "global", "provider_symbol": "GBPUSD=X"},
    "gbpusd": {"symbol": "GBPUSD", "display": "GBP/USD", "asset_class": "fx", "region": "global", "provider_symbol": "GBPUSD=X"},
    "usd/rub": {"symbol": "USDRUB", "display": "USD/RUB", "asset_class": "fx", "region": "global", "provider_symbol": "RUB=X"},
    "usdrub": {"symbol": "USDRUB", "display": "USD/RUB", "asset_class": "fx", "region": "global", "provider_symbol": "RUB=X"},
    "dollar index": {"symbol": "DXY", "display": "US Dollar Index (DXY)", "asset_class": "fx", "region": "US", "provider_symbol": "DX-Y.NYB"},
    "dxy": {"symbol": "DXY", "display": "US Dollar Index (DXY)", "asset_class": "fx", "region": "US", "provider_symbol": "DX-Y.NYB"},
    # --- Rates / Bonds ---
    "us10y": {"symbol": "US10Y", "display": "US 10Y Treasury Yield", "asset_class": "rates", "region": "US", "provider_symbol": "^TNX", "unit": "%"},
    "us 10y": {"symbol": "US10Y", "display": "US 10Y Treasury Yield", "asset_class": "rates", "region": "US", "provider_symbol": "^TNX", "unit": "%"},
    "us 10-year": {"symbol": "US10Y", "display": "US 10Y Treasury Yield", "asset_class": "rates", "region": "US", "provider_symbol": "^TNX", "unit": "%"},
    "us2y": {"symbol": "US2Y", "display": "US 2Y Treasury Yield", "asset_class": "rates", "region": "US", "provider_symbol": "^IRX", "unit": "%"},
    "us 2y": {"symbol": "US2Y", "display": "US 2Y Treasury Yield", "asset_class": "rates", "region": "US", "provider_symbol": "^IRX", "unit": "%"},
    "bund": {"symbol": "DE10Y", "display": "German 10Y Bund Yield", "asset_class": "rates", "region": "DE", "provider_symbol": "^BUND", "unit": "%"},
    "german 10y": {"symbol": "DE10Y", "display": "German 10Y Bund Yield", "asset_class": "rates", "region": "DE", "provider_symbol": "^BUND", "unit": "%"},
    "euribor": {"symbol": "EURIBOR", "display": "EURIBOR", "asset_class": "rates", "region": "EU", "provider_symbol": "EURIBOR", "unit": "%"},
    # --- ETF Proxies ---
    "spy": {"symbol": "SPY", "display": "SPDR S&P 500 ETF", "asset_class": "etf", "region": "US", "provider_symbol": "SPY"},
    "iwm": {"symbol": "IWM", "display": "iShares Russell 2000 ETF", "asset_class": "etf", "region": "US", "provider_symbol": "IWM"},
    "eem": {"symbol": "EEM", "display": "iShares MSCI EM ETF", "asset_class": "etf", "region": "EM", "provider_symbol": "EEM"},
    "gld": {"symbol": "GLD", "display": "SPDR Gold ETF", "asset_class": "etf", "region": "global", "provider_symbol": "GLD"},
    "xle": {"symbol": "XLE", "display": "Energy Select Sector SPDR ETF", "asset_class": "etf", "region": "US", "provider_symbol": "XLE"},
}

_BALTIC_COUNTRIES = {"LT", "LV", "EE"}

# Keys that are too short (<5 chars) or too generic to allow substring matching.
# These are only resolved via exact normalized match (pass 1/2 of _lookup_ticker).
# Pass 3 (substring) explicitly skips them even if they were somehow >= 5 chars.
_EXACT_TICKER_KEYS: frozenset[str] = frozenset({
    "byd", "kia", "nio", "umc", "tsmc", "smic", "catl",
    "amd", "arm", "abb", "sap", "kia",
})

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "last_run.json"

# Section tiers — keyed by URL path's first segment (e.g. /finansai/... -> "finansai")
HIGH_TIER = {
    "finansai", "rinkos", "energetika", "pramone", "statyba-ir-nt",
    "prekyba", "logistika", "inovacijos", "dirbtinis-intelektas",
    "financial-times", "mano-pinigai",
}
CONDITIONAL_TIER = {
    "verslo-aplinka", "mano-verslas", "rinkodara", "vadyba", "izvalgos",
}
SKIP_TIER = {"laisvalaikis", "verslo-klase"}

# Secrets (env)
VZ_EMAIL = os.environ["VZ_EMAIL"]
VZ_PASSWORD = os.environ["VZ_PASSWORD"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
SMTP_TO = os.environ.get("SMTP_TO", SMTP_USER)

PAYWALL_TEXT_MARKERS = ("Žinios, vertos jūsų laiko", "Tapkite prenumeratoriumi")

DISCLAIMER_HTML = (
    '<div style="margin-top:20px;padding:10px 14px;background:#fff8c5;'
    'border:1px solid #d4a72c;border-radius:6px;font-size:12px;color:#57606a;'
    'font-family:-apple-system,Segoe UI,sans-serif;max-width:680px">'
    '⚠️ AI-generated. Verify with primary sources before trading. '
    'Not financial advice.</div>'
)


# State
def load_last_run() -> dt.datetime:
    floor = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=14)
    if not STATE_FILE.exists():
        return floor
    saved = dt.datetime.fromisoformat(json.loads(STATE_FILE.read_text())["last_run_iso"])
    # Always look back at least 14 h so a recent state file can't shrink the window
    return min(saved, floor)


def save_last_run(now: dt.datetime) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_run_iso": now.isoformat()}, indent=2) + "\n")


# RSS
def url_section(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else ""


def fetch_rss(since: dt.datetime) -> list[dict]:
    feed = feedparser.parse(RSS_URL)
    items = []
    for entry in feed.entries:
        if not getattr(entry, "published_parsed", None):
            continue
        pub = dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc)
        if pub <= since:
            continue
        items.append({
            "title": entry.title,
            "url": entry.link,
            "description": entry.get("description", ""),
            "category": entry.get("category", ""),
            "published": pub,
            "section": url_section(entry.link),
        })
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


def tier_filter(items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    high, conditional, skip = [], [], []
    for it in items:
        s = it["section"]
        if s in HIGH_TIER:
            high.append(it)
        elif s in SKIP_TIER:
            skip.append(it)
        elif s in CONDITIONAL_TIER:
            conditional.append(it)
        else:
            conditional.append(it)
    return high, conditional, skip


# Gemini
_client: genai.Client | None = None


def gclient() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# Retryable HTTP codes: 429 rate-limit, 500/502/503/504 transient server errors.
# Anything else (e.g. 400 bad request, 401 auth, 403 quota-permanent) fails fast.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 10
_BASE_DELAY_S = 2.0
_MAX_DELAY_S = 120.0


def _is_retryable(exc: Exception) -> bool:
    if not isinstance(exc, gerrors.APIError):
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _RETRYABLE_CODES:
        return True
    msg = str(exc)
    return any(str(c) in msg for c in _RETRYABLE_CODES)


def _retry_delay_hint(exc: Exception) -> float | None:
    """Parse retryDelay (e.g. '24s') from a 429 APIError body, if present."""
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    for d in (details.get("error", {}) or {}).get("details", []) or []:
        if d.get("@type", "").endswith("RetryInfo"):
            raw = d.get("retryDelay") or ""
            if raw.endswith("s"):
                try:
                    return float(raw[:-1])
                except ValueError:
                    return None
    return None


def gemini_call(
    prompt: str,
    max_tokens: int = 16384,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> str:
    """Call Gemini with retry + model fallback.

    Tries each model in _FALLBACK_MODELS in order. For each, retries
    transient errors (429/5xx) up to _MAX_ATTEMPTS times with backoff.
    Honors retryDelay hint from 429 responses when present.
    """
    cfg_kwargs = {"temperature": temperature, "max_output_tokens": max_tokens}
    if json_mode:
        cfg_kwargs["response_mime_type"] = "application/json"

    last_exc: Exception | None = None
    for model in _FALLBACK_MODELS:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = gclient().models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                if model != _FALLBACK_MODELS[0]:
                    print(f"  gemini fallback succeeded on {model}", file=sys.stderr)
                return (resp.text or "").strip()
            except gerrors.APIError as e:
                last_exc = e
                if not _is_retryable(e):
                    raise
                if attempt == _MAX_ATTEMPTS:
                    print(f"  gemini exhausted retries on {model}, trying next model",
                          file=sys.stderr)
                    break
                hint = _retry_delay_hint(e)
                if hint is not None:
                    delay = min(_MAX_DELAY_S, hint + random.random())
                else:
                    delay = min(_MAX_DELAY_S, _BASE_DELAY_S * (2 ** (attempt - 1)))
                    delay *= 0.5 + random.random()
                code = getattr(e, "code", "?")
                print(f"  gemini transient {code} on {model} (attempt {attempt}/"
                      f"{_MAX_ATTEMPTS}), sleeping {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
    raise last_exc if last_exc else RuntimeError("gemini_call: unreachable")


def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[: -3]
    text = text.strip()
    if text.lower().startswith("json\n"):
        text = text[5:]
    return text.strip()


EXTRACTION_PROMPT = """You are an investment-news analyst processing Lithuanian Verslo žinios articles.
Output ONLY valid JSON matching the schema below. No prose, no code fences, no HTML, no commentary.

═══════════════════════════════════════════
STEP 1 — CLASSIFY each article into exactly one article_type:
direct_public_company | private_company | sector_signal | macro_signal |
commodity_signal | geopolitical_signal | regulation_policy | market_overview |
personal_finance | educational | low_relevance | sponsored_or_ad

═══════════════════════════════════════════
STEP 2 — FILTER. Output these as low_relevance with skip_reason (minimal fields only):
• Sponsored / "Rekomenduojame" / "Verslo tribūna" / advertorial → skip_reason: "sponsored"
• Lifestyle, gastronomy, culture, travel → skip_reason: "lifestyle"
• Generic educational explainers with no near-term tradable signal → skip_reason: "educational"
• Narrow legal/court news with no investable implication → skip_reason: "legal, no market impact"
• Crime, culture, non-market politics → skip_reason: "not market-relevant"

Conditionally include (only with clear market/regulation/sector angle):
VERSLO APLINKA, VADYBA, ĮŽVALGOS, MANO VERSLAS, RINKODARA, MANO PINIGAI

Always include: FINANSAI, RINKOS, STATYBA IR NT, PRAMONĖ, ENERGETIKA,
PREKYBA, LOGISTIKA, INOVACIJOS, DIRBTINIS INTELEKTAS, FINANCIAL TIMES

═══════════════════════════════════════════
STEP 3 — DEDUP. If two URLs cover the same story (same company + same event):
• Mark the shorter/digest version as low_relevance with skip_reason: "duplicate: prefer <other URL>"
• Never produce two cards for the same company + event theme.
• Liveblogs ("Dienos pulsas", "Dienos akcentai", timestamped tickers) → set is_liveblog=true.
  They go in a separate section; do NOT treat them as main cards.

═══════════════════════════════════════════
STEP 4 — IMPORTANCE (target 4–6 HIGH per run):
HIGH:
  • Public company earnings/guidance/M&A with direct price impact
  • Oil/rates/inflation shock with clear transmission mechanism
  • Listed Baltic company result or major trading move
  • Major sector-wide data with concrete investable conclusion
MEDIUM:
  • Private company investment ≥ €50M
  • Sector stress or structural shift
  • Local regulation with real cost/revenue impact
  • Indirect macro relevance
LOW → skip or Watchlist:
  • Generic education, lifestyle, weak opinion, sponsored
  • Narrow legal with no investable implication
  • Vague bilateral trade
  • Private local business story without scale

═══════════════════════════════════════════
STEP 5 — EVIDENCE SNIPPETS:
• importance=high: provide 4–8 snippets. importance=medium: provide 2–4 snippets.
• Each snippet: verbatim Lithuanian substring of THAT article's body. MAX 12 WORDS.
• Prefer snippets with numbers; cover different facts (not variations of the same sentence).
• No paraphrasing, no translation, no full paragraphs.
• If you cannot find ≥2 snippets, output item as low_relevance with skip_reason: "insufficient evidence".
GOOD: "apie 200 mln. Eur Kairių"
BAD:  "2026–2027 metais didieji projektai turės daug įtakos viso sektoriaus rodikliams..."

═══════════════════════════════════════════
STEP 6 — TICKERS (with roles):
candidate_companies must be a JSON array of objects: [{"name": "...", "role": "..."}]
Roles:
  direct          = company is the primary subject of the event (earnings, M&A, IPO, regulation on them)
  related         = company is directly affected or competing (e.g. rival mentioned)
  background      = mentioned only for industry context
  comparison_only = used only as a comparison benchmark, not actually involved
  private         = known to be private (SpaceX, OpenAI, etc.)

ONLY companies with role=direct appear in the Direct Public Tickers table (section C).
related/background tickers are shown inside the card only.
• Name companies exactly as written in the article.
• ticker_proxies: companies NOT mentioned in the article but useful as sector proxies.
• Never invent ticker symbols — Python validates names against a static map.

═══════════════════════════════════════════
STEP 6B — MARKET INSTRUMENTS:
If the article discusses macro instruments (equity indices, commodities, crypto, FX pairs,
interest rates, bond yields) with directional context, list them in candidate_instruments.
• direction: how the article implies the instrument is moving or will move.
  bullish  = rising / positive outlook for that instrument
  bearish  = falling / negative outlook
  mixed    = conflicting signals in same article
  neutral  = mentioned without clear direction
  unclear  = cannot determine from article text
• Examples:
  "Brent zemiau 80 USD/bbl" → {"name": "Brent", "direction": "bearish"}
  "S&P 500 pasieke rekordini maksimuma" → {"name": "S&P 500", "direction": "bullish"}
  "JAV 10 metu obligaciju pajamingumas kyla" → {"name": "US10Y", "direction": "bearish"}
  "EUR/USD kursas stabilizavosi" → {"name": "EUR/USD", "direction": "neutral"}
• Do NOT place indices, crypto, commodities, FX pairs, or rates into candidate_companies.
• Omit candidate_instruments (or leave as []) if no macro instruments appear with
  directional context.

═══════════════════════════════════════════
STEP 7 — SIGNALS:
• signal_fundamental: your analysis of the event's investment quality.
  Values: bullish | bearish | mixed | neutral | unclear
• signal_market_reaction: ONLY if article text explicitly states how a stock/index/asset moved.
  Values: positive | negative | neutral | unknown
  DEFAULT = "unknown". Do NOT guess market reaction.

═══════════════════════════════════════════
STEP 8 — EXECUTIVE BRIEF BULLET (brief_bullet field):
Write ONE complete English sentence:
• ≤22 words. NO ellipsis. NO trailing "…". Complete sentence, full stop.
• Must contain a concrete number OR a clear market implication.
• Write like a human editor, not an AI summary.
GOOD: "Norway resumes gas field production, adding ~2 bcm/year to Europe's supply buffer."
BAD:  "Norway resumes exploitation of several fields, boosting gas supply to Europe..."

═══════════════════════════════════════════
STEP 9 — CONFIDENCE:
high   = hard data from the article (earnings, official figures, signed agreements)
medium = analysis, estimates, unnamed sources, preliminary/proposed changes
low    = speculation, opinion, historical analogy, vague statements
Not everything is HIGH. Opinion pieces, historical comparisons, and speculative
macro commentary must be CONF:LOW or CONF:MEDIUM.

═══════════════════════════════════════════
STEP 10 — TRADABILITY:
direct     = listed company directly affected; can be traded now
indirect   = sector/macro effect; tradable via ETF or proxy
watch-only = private, state-owned, or no clear near-term tradable instrument

═══════════════════════════════════════════
STEP 11 — MARKET READ (market_read field):
Write 2–3 English sentences explaining why this event matters for investors.
• Cover both the short-term signal and relevant long-term context.
• No BUY/SELL/HOLD. No filler phrases ("This is significant because…").
• Be specific: name the mechanism, the affected asset class, the magnitude.
• Every number you use in market_read MUST appear verbatim in evidence_lt. If you cannot
  trace a number to evidence, omit it from market_read.

═══════════════════════════════════════════
STEP 12 — LIVEBLOG SUBITEMS (only when is_liveblog=true):
Split the liveblog into individual timestamped entries. Output each as a subitem.
• Include only entries with an investable angle (price move, company result, macro data).
• Omit purely political/social/sports entries.
• score_hint: your estimate of investment relevance 0–100 (use the same rubric as importance).
  0=irrelevant, 40=watchlist-grade, 70+=top-signal-grade.

═══════════════════════════════════════════
HARD RULES (any violation = drop the item in Python):
• url must exactly match the URL provided for that article.
• Every evidence_lt snippet must be a verbatim substring of THAT article's body. ≤12 words.
• Never output BUY / SELL / HOLD or analyst price targets or ratings.
• Never invent tickers — list company names in candidate_companies exactly as written.
• Two articles covering the same event → only one gets a full card.

═══════════════════════════════════════════
JSON SCHEMA (one object per article):
{
  "url": "<exact URL provided>",
  "article_type": "<type from STEP 1>",
  "is_liveblog": false,
  "importance": "high|medium|low",
  "confidence": "high|medium|low",
  "headline_en": "<≤12 words>",
  "brief_bullet": "<≤22 words, complete sentence, no ellipsis, number or market implication>",
  "evidence_lt": ["<verbatim LT substring, ≤12 words, prefer numeric snippets>", ...],
  "market_read": "<2–3 EN sentences; numbers must appear in evidence_lt; no BUY/SELL/HOLD>",
  "candidate_companies": [{"name": "<as written>", "role": "direct|related|background|comparison_only|private"}, ...],
  "candidate_instruments": [{"name": "<instrument as mentioned>", "direction": "bullish|bearish|mixed|neutral|unclear"}],
  "ticker_proxies": ["<company not in article but useful sector proxy>"],
  "affected_direct": ["<companies/sectors/countries directly involved>"],
  "affected_indirect": ["<assets/markets possibly affected>"],
  "signal_fundamental": "bullish|bearish|mixed|neutral|unclear",
  "signal_market_reaction": "positive|negative|neutral|unknown",
  "tradability": "direct|indirect|watch-only",
  "skip_reason": "<only for low_relevance/sponsored/duplicate, else omit>",
  "liveblog_subitems": [
    {
      "time": "<HH:MM or empty string>",
      "headline_en": "<≤10 word English summary of this entry>",
      "brief_lt": "<verbatim LT snippet from this entry, ≤15 words>",
      "entities": ["<company or country name>"],
      "signal": "bullish|bearish|neutral|unclear",
      "score_hint": 0
    }
  ]
}
Note: liveblog_subitems is ONLY populated when is_liveblog=true. Omit for normal articles.

Output exactly: {"items": [<item>, <item>, ...]}

ARTICLES:
"""


def gemini_extract(articles: list[dict]) -> dict:
    block = ""
    for a in articles:
        body = a["body"][:3500]
        block += f"\n\n=== URL: {a['url']} ===\n=== TITLE: {a['title']} ===\n{body}"
    raw = gemini_call(
        EXTRACTION_PROMPT + block,
        max_tokens=32768,
        temperature=0.3,
        json_mode=True,
    )
    text = strip_fences(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i:j + 1])
        raise


def normalize_text(s: str) -> str:
    return " ".join(s.split()).lower()


_DROP_TYPES = {"low_relevance", "sponsored_or_ad"}
_IMPORTANCE_RANK = {"high": 0, "medium": 1, "low": 2}
_MAX_TOP = 6        # max cards in Top Signals (B)
_MAX_WATCHLIST = 8  # max cards in Watchlist (D)

# Score thresholds
_SCORE_TOP = 60           # minimum score for Top Signals
_SCORE_TOP_FALLBACK = 50  # pulled up when fewer than 4 items hit 60
_SCORE_WATCH = 35         # minimum score for Watchlist
_SCORE_BRIEF = 40         # minimum subitem score to appear in Executive Brief

# Article type base scores
_TYPE_SCORE: dict[str, int] = {
    "direct_public_company": 30,
    "macro_signal": 25,
    "commodity_signal": 25,
    "geopolitical_signal": 20,
    "regulation_policy": 20,
    "sector_signal": 15,
    "private_company": 15,
    "market_overview": 0,
    "educational": 0,
    "personal_finance": 0,
    "low_relevance": 0,
    "sponsored_or_ad": 0,
}

# Article type penalties (applied on top of base)
_TYPE_PENALTY: dict[str, int] = {
    "sponsored_or_ad": -25,
    "market_overview": -15,
    "educational": -10,
    "personal_finance": -10,
}

# These article types should never lead Top Signals.
_OPINION_TYPES = {"educational", "personal_finance", "market_overview"}


def compute_investment_score(item: dict) -> int:
    """Score 0-100 based on article type, Baltic status, evidence quality, market reaction."""
    a_type = (item.get("article_type") or "").lower()
    react = (item.get("signal_market_reaction") or "unknown").lower()
    evidence = item.get("evidence_lt") or []
    skip = (item.get("skip_reason") or "").lower()

    score = _TYPE_SCORE.get(a_type, 10)
    score += _TYPE_PENALTY.get(a_type, 0)

    # Baltic listed company bonus
    if item.get("is_baltic"):
        score += 20

    # Concrete numbers present in evidence
    if any(re.search(r"\d", s) for s in evidence):
        score += 10

    # Explicit market reaction stated in article
    if react in {"positive", "negative", "neutral"}:
        score += 10

    # Lifestyle / culture penalty on low_relevance
    if a_type == "low_relevance" and any(
        w in skip for w in ("lifestyle", "culture", "gastronomy", "travel")
    ):
        score -= 20

    return max(0, min(100, score))


def compute_subitem_score(sub: dict) -> int:
    """Score a liveblog subitem using Gemini's score_hint + entity type adjustments."""
    score = max(0, min(100, int(sub.get("score_hint") or 40)))
    # Baltic public company bonus
    for name in sub.get("entities") or []:
        info = _lookup_ticker(name)
        if info and info.get("country") in _BALTIC_COUNTRIES:
            score = min(100, score + 10)
            break
    if (sub.get("signal") or "unclear").lower() == "unclear":
        score = max(0, score - 10)
    return score


def _dedup_snippets(snippets: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for s in snippets:
        norm = normalize_text(s)
        if norm not in seen:
            seen.add(norm)
            result.append(s)
    return result


def _is_incomplete_numeric(snippet: str) -> bool:
    """True when the snippet ends with a bare decimal/percent token with no unit/context."""
    tokens = snippet.strip().rstrip(".,;").split()
    if not tokens:
        return False
    last = tokens[-1]
    # Bare number or percentage at end with ≤3 tokens total → incomplete
    return bool(re.match(r"^[\d,.]+%?$", last)) and len(tokens) <= 3


def _extract_number_tokens(text: str) -> set[str]:
    return set(re.findall(r"\b\d[\d,./]*%?\b", text or ""))


def _validate_numbers_against_evidence(item: dict) -> None:
    """
    If a number in brief_bullet or market_read cannot be traced to evidence_lt,
    replace the field with a safe fallback to prevent cross-article contamination.
    """
    evidence_joined = " ".join(item.get("evidence_lt") or [])
    for field in ("brief_bullet", "market_read"):
        text = item.get(field) or ""
        unsupported = [
            n for n in _extract_number_tokens(text)
            if n not in evidence_joined
        ]
        if unsupported:
            print(f"  num-contamination {field} {unsupported}: {item.get('headline_en')}")
            if field == "brief_bullet":
                # Fall back to headline — safer than a fabricated number
                headline = (item.get("headline_en") or "").strip()
                item["brief_bullet"] = headline if headline.endswith(".") else headline + "."
            else:
                # Strip the offending numbers inline (crude but safe)
                for n in unsupported:
                    text = re.sub(r"\b" + re.escape(n) + r"\b", "[N]", text)
                item[field] = text


# Common legal / corporate suffixes stripped before matching so that
# “Visa Inc”, “SAP SE”, “Neste Oyj” all resolve to their base key.
_CORP_SUFFIX_RE = re.compile(
    r"\s+(?:inc|corp|corporation|ltd|limited|plc|ag|se|ab|oy|oyj|asa|bv|nv"
    r"|sa|sas|srl|spa|llc|lp|gmbh|co|holding|holdings|international"
    r"|technologies?|solutions|services|enterprises?|ventures?)\.?$",
    re.IGNORECASE,
)


def _normalize_company_name(name: str) -> str:
    """Lowercase, strip LT quotes/punctuation, remove trailing corporate suffixes."""
    if not name:
        return ""
    name = name.lower()
    for old, new in [
        ("\u201e", ""), ("\u201c", ""), ("\u201d", ""), ('"', ""),
        ("\u2018", ""), ("\u2019", ""), ("\u02bc", ""),
        ("\u2013", "-"), ("\u2014", "-"), ("\u2026", ""),
    ]:
        name = name.replace(old, new)
    name = re.sub(r"\s+", " ", name).strip()
    # Strip trailing corporate suffix (iteratively, e.g. "Visa Inc Corp" -> "Visa")
    while True:
        stripped = _CORP_SUFFIX_RE.sub("", name).strip()
        if stripped == name:
            break
        name = stripped
    return name

def _remove_accents(text: str) -> str:
    """Strip combining diacritics (ą→a, š→s, ž→z, etc.)."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


# Geographic / subsidiary qualifiers that indicate a local branch, not the listed parent.
# If the query contains one of these AND the candidate key does not, the substring match
# is skipped to prevent "Orlen Lietuva" matching "orlen" (PKN) or
# "Circle K Lietuva" matching an unrelated key.
_SUBSIDIARY_QUALIFIERS = {
    "lietuva", "latvija", "eesti", "estonia", "latvia", "lithuania",
    "polska", "polska", "suomi", "finland", "sverige", "sweden",
    "deutschland", "germany", "france", "espana", "italia",
}


def _lookup_ticker(name: str) -> dict | None:
    """
    Three-pass lookup against TICKER_MAP:
    1. Normalized exact match (lowercase, strip LT quotes/corporate suffixes)
    2. Accent-insensitive exact match (ą→a, š→s, ž→z, etc.)
    3. Substring match — keys ≥5 chars only, AND not in _EXACT_TICKER_KEYS.
       Keys in _EXACT_TICKER_KEYS (short/ambiguous: tsmc, byd, kia, catl, …)
       only resolve via passes 1–2 to avoid false-positive substring hits.
       If the query contains a geographic qualifier (e.g. "lietuva") but the
       candidate key does not, the match is skipped so that
       "Orlen Lietuva" never resolves to the PKN (parent) ticker.
    """
    if not name:
        return None
    query = _normalize_company_name(name)
    query_na = _remove_accents(query)

    # Build normalized view of the map (small dict, cheap)
    norm_map = {_normalize_company_name(k): v for k, v in TICKER_MAP.items()}

    # Pass 1: exact normalized match (works for any length, including short keys)
    if query in norm_map:
        return norm_map[query]

    # Pass 2: accent-insensitive exact match
    for key, value in norm_map.items():
        if _remove_accents(key) == query_na:
            return value

    # Determine if query contains a subsidiary qualifier
    query_words = set(query.split())
    query_qualifiers = query_words & _SUBSIDIARY_QUALIFIERS

    # Pass 3: substring match — only long, unambiguous keys
    for key, value in norm_map.items():
        # Never substring-match short/ambiguous keys
        if key in _EXACT_TICKER_KEYS:
            continue
        if len(key) < 5:
            continue
        # Subsidiary guard: skip if the query is qualified but the key is not,
        # e.g. don't let "orlen lietuva" match "orlen"
        if query_qualifiers:
            key_words = set(key.split())
            if not (key_words & _SUBSIDIARY_QUALIFIERS):
                continue
        key_na = _remove_accents(key)
        if key in query or query in key:
            return value
        if key_na in query_na or query_na in key_na:
            return value

    return None


def _normalize_instrument_name(name: str) -> str:
    """Lowercase, collapse whitespace. Preserves / so EUR/USD stays intact."""
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    return name.rstrip(".,;:")


def _lookup_instrument(name: str) -> dict | None:
    """Two-pass lookup against INSTRUMENT_MAP: exact normalized, then substring (key >= 4 chars)."""
    if not name:
        return None
    query = _normalize_instrument_name(name)
    query_na = _remove_accents(query)

    norm_map = {_normalize_instrument_name(k): v for k, v in INSTRUMENT_MAP.items()}

    # Pass 1: exact normalized match
    if query in norm_map:
        return norm_map[query]

    # Pass 2: accent-insensitive exact match
    for key, value in norm_map.items():
        if _remove_accents(key) == query_na:
            return value

    # Pass 3: substring match (keys >= 4 chars)
    for key, value in norm_map.items():
        if len(key) < 4:
            continue
        key_na = _remove_accents(key)
        if key in query or query in key:
            return value
        if key_na in query_na or query_na in key_na:
            return value

    return None


def _theme_key(item: dict) -> str:
    """Group items by primary entity for theme-based dedup."""
    entity = ((item.get("affected_direct") or [""])[0]).strip().lower()[:40]
    sig_words = sorted(
        w.lower() for w in (item.get("headline_en") or "").split() if len(w) > 4
    )[:4]
    return entity + "|" + " ".join(sig_words)


def validate(extracted: dict, articles: list[dict]) -> dict:
    """
    Returns {"top_signals", "watchlist", "liveblogs", "skipped"}.
    Selection is score-based (investment_score), not just Gemini importance labels.
    Liveblogs are split into scored subitems; cross-dedup prevents double coverage.
    """
    by_url = {a["url"]: a for a in articles}
    raw_items = extracted.get("items", []) or []
    skipped: list[dict] = []

    def _skip(item, reason):
        skipped.append({
            "headline_en": item.get("headline_en") or item.get("url") or "—",
            "article_type": item.get("article_type") or "—",
            "reason": reason,
        })

    # ── Pass 1: basic validation ──────────────────────────────────────
    passed: list[dict] = []
    for item in raw_items:
        a_type = (item.get("article_type") or "").strip().lower()

        if a_type in _DROP_TYPES:
            _skip(item, item.get("skip_reason") or a_type)
            continue

        a = by_url.get(item.get("url"))
        if not a:
            _skip(item, "unknown url")
            continue

        body_norm = normalize_text(a["body"])

        # Evidence: trim to 12 words, verify substring in body, dedup, reject incomplete numerics
        snippets = [s for s in (item.get("evidence_lt") or []) if s]
        trimmed = [" ".join(s.split()[:12]) for s in snippets]
        valid_snips = [s for s in trimmed if normalize_text(s) in body_norm]
        valid_snips = _dedup_snippets(valid_snips)
        valid_snips = [s for s in valid_snips if not _is_incomplete_numeric(s)]
        if len(valid_snips) < 2 and not item.get("is_liveblog"):
            _skip(item, "fewer than 2 evidence snippets verified in body")
            continue
        item["evidence_lt"] = valid_snips[:8]

        # Ticker resolution — candidate_companies is now list of {name, role} objects
        public, companies_private, tickers_unclear = [], [], []
        seen_tickers: set[str] = set()
        seen_names: set[str] = set()
        raw_companies = item.get("candidate_companies") or []
        for co in raw_companies:
            if isinstance(co, dict):
                name = (co.get("name") or "").strip()
                role = (co.get("role") or "direct").strip().lower()
            else:
                name = str(co).strip()
                role = "direct"
            if not name:
                continue
            key = name.lower()
            info = _lookup_ticker(name)
            if info is None:
                if key not in seen_names:
                    tickers_unclear.append({"name": name, "role": role})
                    seen_names.add(key)
            elif info["ticker"].startswith("N/A"):
                if key not in seen_names:
                    companies_private.append({"name": name, "role": role})
                    seen_names.add(key)
            else:
                t = info["ticker"]
                if t not in seen_tickers:
                    public.append({**info, "name": name, "role": role})
                    seen_tickers.add(t)

        item["public_tickers"] = public
        item["companies_private"] = companies_private
        item["tickers_unclear"] = tickers_unclear
        item["is_baltic"] = any(p.get("country") in _BALTIC_COUNTRIES for p in public)

        # Instrument resolution — candidate_instruments lists macro instruments (indices, FX, etc.)
        raw_instruments = item.get("candidate_instruments") or []
        resolved_instr: list[dict] = []
        seen_instr_syms: set[str] = set()
        for instr in raw_instruments:
            if isinstance(instr, dict):
                iname = (instr.get("name") or "").strip()
                idirection = (instr.get("direction") or "unclear").strip().lower()
            else:
                iname = str(instr).strip()
                idirection = "unclear"
            if not iname:
                continue
            iinfo = _lookup_instrument(iname)
            if iinfo:
                isym = iinfo.get("symbol", "")
                if isym and isym not in seen_instr_syms:
                    resolved_instr.append({**iinfo, "direction": idirection, "raw_name": iname})
                    seen_instr_syms.add(isym)
        item["resolved_instruments"] = resolved_instr

        # Opinion types: cap confidence at medium
        if a_type in _OPINION_TYPES:
            if (item.get("confidence") or "low").lower() == "high":
                item["confidence"] = "medium"

        # Fix ellipsis in brief_bullet
        bullet = (item.get("brief_bullet") or "").strip()
        if bullet.endswith("…") or bullet.endswith("..."):
            bullet = bullet.rstrip(".… ").rstrip(",").rstrip() + "."
            item["brief_bullet"] = bullet

        # Number contamination check: numbers in brief_bullet/market_read must be in evidence
        _validate_numbers_against_evidence(item)

        passed.append(item)

    # ── Pass 2: headline-prefix dedup ────────────────────────────────
    by_headline: dict[str, dict] = {}
    for item in passed:
        key = (item.get("headline_en") or "").strip().lower()[:30]
        if not key:
            by_headline[item.get("url", str(id(item)))] = item
            continue
        existing = by_headline.get(key)
        if existing is None:
            by_headline[key] = item
        else:
            ei = _IMPORTANCE_RANK.get((existing.get("importance") or "low").lower(), 3)
            ii = _IMPORTANCE_RANK.get((item.get("importance") or "low").lower(), 3)
            if ii < ei:
                _skip(existing, "dedup: superseded by higher-importance variant")
                by_headline[key] = item
            else:
                _skip(item, "dedup: duplicate headline")
    deduped = list(by_headline.values())

    # ── Pass 3: theme-based dedup (same entity + similar headline words) ─
    theme_map: dict[str, dict] = {}
    final_passed: list[dict] = []
    for item in deduped:
        tk = _theme_key(item)
        existing = theme_map.get(tk)
        if existing is None:
            theme_map[tk] = item
            final_passed.append(item)
        else:
            if len(item.get("evidence_lt") or []) > len(existing.get("evidence_lt") or []):
                _skip(existing, "theme-dedup: merged into richer card")
                theme_map[tk] = item
                final_passed = [i for i in final_passed if i is not existing]
                final_passed.append(item)
            else:
                _skip(item, "theme-dedup: merged into richer card")

    # ── Pass 4: compute investment_score for every item ───────────────
    for item in final_passed:
        item["investment_score"] = compute_investment_score(item)

    # ── Pass 5: separate liveblogs / non-liveblogs ────────────────────
    liveblog_articles = [i for i in final_passed if i.get("is_liveblog")]
    non_liveblogs = [i for i in final_passed if not i.get("is_liveblog")]

    # Sort non-liveblogs by score desc, Baltic first within same score
    non_liveblogs.sort(key=lambda x: (-x["investment_score"], 0 if x.get("is_baltic") else 1))

    # ── Pass 6: score-based section assignment ────────────────────────
    top_candidates = [i for i in non_liveblogs if i["investment_score"] >= _SCORE_TOP]
    below_top = [i for i in non_liveblogs if i["investment_score"] < _SCORE_TOP]

    # Enforce 4-8 evidence snippets for Top Signal candidates; demote if insufficient
    top_signals_pre: list[dict] = []
    for item in top_candidates:
        if len(item.get("evidence_lt") or []) < 4:
            item["investment_score"] = max(0, item["investment_score"] - 10)
            below_top.append(item)
            print(f"  demoted (evidence <4 for top signal): {item.get('headline_en')}")
        else:
            top_signals_pre.append(item)
    below_top.sort(key=lambda x: -x["investment_score"])

    # Fallback: if fewer than 4 reach _SCORE_TOP, pull up items >= _SCORE_TOP_FALLBACK
    if len(top_signals_pre) < 4:
        for item in below_top[:]:
            if len(top_signals_pre) >= 4:
                break
            if item["investment_score"] >= _SCORE_TOP_FALLBACK and len(item.get("evidence_lt") or []) >= 2:
                top_signals_pre.append(item)
                below_top.remove(item)
                print(f"  pulled to top (fallback ≥{_SCORE_TOP_FALLBACK}): {item.get('headline_en')}")

    # Cap Top Signals at _MAX_TOP; overflow goes back to below_top pool
    top_signals = top_signals_pre[:_MAX_TOP]
    overflow = top_signals_pre[_MAX_TOP:]
    for item in overflow:
        below_top.append(item)
    below_top.sort(key=lambda x: -x["investment_score"])

    # Build watchlist from remaining items scoring >= _SCORE_WATCH, in score order
    watchlist = [i for i in below_top if i["investment_score"] >= _SCORE_WATCH]
    skipped_low = [i for i in below_top if i["investment_score"] < _SCORE_WATCH]

    for item in skipped_low:
        _skip(item, f"score {item['investment_score']} below threshold {_SCORE_WATCH}")

    # Cap watchlist at _MAX_WATCHLIST (already sorted by score, so lowest-score items drop)
    for item in watchlist[_MAX_WATCHLIST:]:
        _skip(item, f"cap: score {item['investment_score']}")
    watchlist = watchlist[:_MAX_WATCHLIST]

    # Rule 10: if 3+ items passed threshold, must have ≥2 Top Signals unless all others <50
    total_passing = len(top_signals) + len(watchlist)
    if total_passing >= 3 and len(top_signals) < 2:
        for item in list(watchlist):
            if item["investment_score"] >= _SCORE_TOP_FALLBACK:
                top_signals.append(item)
                watchlist.remove(item)
                print(f"  promoted to top_signals (rule 10): {item.get('headline_en')}")
                break

    # ── Pass 7: collect entities covered by full article cards ────────
    covered_entities: set[str] = set()
    for item in top_signals + watchlist:
        for d in item.get("affected_direct") or []:
            covered_entities.add(d.strip().lower())
        for co in item.get("candidate_companies") or []:
            name = co.get("name", "") if isinstance(co, dict) else str(co)
            covered_entities.add(name.strip().lower())

    # ── Pass 8: extract + score liveblog subitems; cross-dedup ───────
    market_subitems: list[dict] = []
    for lb_art in liveblog_articles:
        for sub in lb_art.get("liveblog_subitems") or []:
            sub["_parent_url"] = lb_art.get("url", "")
            sub["investment_score"] = compute_subitem_score(sub)
            # Check cross-dedup: is any entity already covered by a full article?
            sub_entities = {e.strip().lower() for e in (sub.get("entities") or [])}
            if sub_entities & covered_entities:
                skipped.append({
                    "headline_en": sub.get("headline_en", "—"),
                    "article_type": "liveblog_subitem",
                    "reason": "covered by full article card",
                })
            elif sub["investment_score"] >= _SCORE_WATCH:
                market_subitems.append(sub)

        # Also add covered_entities from any high-score subitems we're keeping
        for sub in market_subitems:
            for e in sub.get("entities") or []:
                covered_entities.add(e.strip().lower())

    market_subitems.sort(key=lambda x: (x.get("time") or "00:00"))

    # ── Pass 9: aggregate market instruments across all validated items ─────
    instruments_by_symbol: dict[str, dict] = {}
    for item in top_signals + watchlist:
        for ri in (item.get("resolved_instruments") or []):
            sym = ri.get("symbol", "")
            if not sym:
                continue
            if sym not in instruments_by_symbol:
                instruments_by_symbol[sym] = {
                    k: v for k, v in ri.items() if k not in ("direction", "raw_name")
                }
                instruments_by_symbol[sym]["directions"] = []
                instruments_by_symbol[sym]["evidence_articles"] = []
            instruments_by_symbol[sym]["directions"].append(ri.get("direction", "unclear"))
            instruments_by_symbol[sym]["evidence_articles"].append({
                "url": item.get("url", ""),
                "headline_en": item.get("headline_en", ""),
                "evidence_lt": (item.get("evidence_lt") or [])[:3],
                "market_read": item.get("market_read", ""),
                "affected_direct": item.get("affected_direct") or [],
                "affected_indirect": item.get("affected_indirect") or [],
            })

    def _merge_directions(dirs: list[str]) -> str:
        real = {d for d in dirs if d != "unclear"}
        if not real:
            return "unclear"
        if len(real) == 1:
            return next(iter(real))
        return "mixed"

    _INSTR_CLASS_ORDER = {
        "equity_index": 0, "volatility_index": 1, "commodity": 2,
        "fx": 3, "rates": 4, "crypto": 5, "etf": 6,
    }
    instruments_list: list[dict] = []
    for sym, data in instruments_by_symbol.items():
        data["direction"] = _merge_directions(data["directions"])
        instruments_list.append(data)
    instruments_list.sort(key=lambda x: (
        _INSTR_CLASS_ORDER.get(x.get("asset_class", ""), 99),
        x.get("symbol", ""),
    ))

    return {
        "top_signals": top_signals,
        "watchlist": watchlist,
        "liveblogs": market_subitems,
        "instruments": instruments_list,
        "skipped": skipped,
    }


_FUND_COLORS = {
    "bullish": "#1a7f37", "bearish": "#cf222e",
    "mixed": "#9a6700", "neutral": "#57606a", "unclear": "#8c959f",
}
_REACT_COLORS = {
    "positive": "#1a7f37", "negative": "#cf222e",
    "neutral": "#57606a", "unknown": "#8c959f",
}
_TRADE_COLORS = {
    "direct": "#0969da", "indirect": "#9a6700", "watch-only": "#57606a",
}

_F = "-apple-system,Segoe UI,Roboto,sans-serif"


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _join(items, sep=" · "):
    return sep.join(_esc(x) for x in items if x) if items else "—"


def _row(icon: str, label: str, content: str) -> str:
    return (f'<div style="margin:0 0 9px;font-size:13px;line-height:1.5">'
            f'<strong>{icon} {label}:</strong> {content}</div>')


def _badge(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;background:{color};color:#fff;'
            f'padding:2px 9px;border-radius:4px;font-weight:600;font-size:12px;'
            f'margin-right:5px;white-space:nowrap">{_esc(text)}</span>')


def _render_card(item: dict) -> str:
    a_type = _esc((item.get("article_type") or "other").replace("_", " ").upper())
    importance = _esc((item.get("importance") or "—").upper())
    confidence = _esc(item.get("confidence") or "—")
    liveblog = (' · <strong style="color:#cf222e">🔴 LIVEBLOG</strong>'
                if item.get("is_liveblog") else "")

    headline = _esc(item.get("headline_en") or "")
    url = _esc(item.get("url") or "")

    snips = (item.get("evidence_lt") or [])[:8]
    ev_items = "".join(
        f'<div style="margin:3px 0">• "{_esc(s)}"</div>' for s in snips
    )

    public = item.get("public_tickers") or []
    priv = item.get("companies_private") or []
    unclear = item.get("tickers_unclear") or []
    proxies = item.get("ticker_proxies") or []

    def _ticker_label(p: dict) -> str:
        role = (p.get("role") or "direct").lower()
        role_suffix = (
            f' <span style="font-size:10px;color:#9a6700;font-weight:400">[{_esc(role)}]</span>'
            if role != "direct" else ""
        )
        return (
            f'<strong>{_esc(p["ticker"])}</strong>'
            f'<span style="color:#8c959f"> ({_esc(p["exchange"])})</span>'
            f'{role_suffix}'
        )

    tickers_verified = (", ".join(_ticker_label(p) for p in public) if public else "—")

    fund = (item.get("signal_fundamental") or "unclear").lower()
    react = (item.get("signal_market_reaction") or "unknown").lower()
    trade = (item.get("tradability") or "watch-only").lower()

    affected_parts = []
    direct = item.get("affected_direct")
    indirect = item.get("affected_indirect")
    if direct:
        affected_parts.append(f'<strong>Direct:</strong> {_join(direct)}')
    if indirect:
        affected_parts.append(f'<strong>Indirect:</strong> {_join(indirect)}')
    affected_html = (' &nbsp;|&nbsp; '.join(affected_parts)) if affected_parts else "—"

    return (
        f'<div style="border:1px solid #444c56;border-radius:8px;padding:16px 18px;'
        f'margin:0 0 16px;font-family:{_F};max-width:680px">'

        # header: type · importance · conf + headline + evidence
        f'<div style="border-bottom:1px dashed #444c56;padding-bottom:12px;margin-bottom:14px">'
        f'<div style="font-size:11px;color:#8c959f;text-transform:uppercase;'
        f'letter-spacing:.5px;margin-bottom:6px">📰 {a_type} · {importance} · '
        f'score:{item.get("investment_score", "?")} · conf:{confidence}{liveblog}</div>'
        f'<div style="margin:0 0 10px;font-size:15px;font-weight:600;line-height:1.35">'
        f'<a href="{url}" style="color:#4493f8;text-decoration:none">{headline}</a></div>'
        + (f'<div style="padding:8px 12px;border-left:3px solid #444c56;'
           f'color:#8c959f;font-size:13px;font-style:italic">{ev_items}</div>'
           if ev_items else '')
        + '</div>'

        # body
        + _row("🌐", "Market read", _esc(item.get("market_read") or ""))
        + f'<div style="margin:0 0 9px;font-size:13px;line-height:1.8">'
          f'<strong>📈 Signal:</strong> '
          + _badge(f"Fundamental: {fund}", _FUND_COLORS.get(fund, "#57606a"))
          + _badge(f"Market: {react}", _REACT_COLORS.get(react, "#8c959f"))
          + _badge(f"Tradability: {trade}", _TRADE_COLORS.get(trade, "#57606a"))
          + '</div>'
        + _row("🎯", "Affected", affected_html)
        + _row("📊", "Tickers", tickers_verified)
        + (_row("🏢", "Private", _join(p.get("name", "?") for p in priv)) if priv else "")
        + (_row("❓", "Ticker unclear", _join(u.get("name", "?") for u in unclear)) if unclear else "")
        + (_row("🔭", "Proxies", _join(proxies)) if proxies else "")
        + '</div>'
    )


def _render_watchlist_row(item: dict) -> str:
    """Compact card for section D (Watchlist): brief bullet + 2-4 evidence snippets."""
    importance = (item.get("importance") or "medium").upper()
    headline = _esc(item.get("headline_en") or "")
    url = _esc(item.get("url") or "")
    a_type = _esc((item.get("article_type") or "").replace("_", " "))
    brief = _esc(item.get("brief_bullet") or "")
    fund = (item.get("signal_fundamental") or "neutral").lower()
    trade = (item.get("tradability") or "watch-only").lower()
    color = _FUND_COLORS.get(fund, "#57606a")

    snips = (item.get("evidence_lt") or [])[:4]
    ev_items = "".join(
        f'<div style="margin:2px 0;font-size:12px;color:#8c959f;font-style:italic">• "{_esc(s)}"</div>'
        for s in snips
    )

    return (
        f'<div style="border-left:3px solid #444c56;padding:8px 12px;'
        f'margin:0 0 10px;font-family:{_F}">'
        f'<div style="font-size:11px;color:#8c959f;margin-bottom:3px">'
        f'{importance} · {_esc(a_type)}</div>'
        f'<div style="font-size:14px;font-weight:600;margin-bottom:4px">'
        f'<a href="{url}" style="color:#4493f8;text-decoration:none">{headline}</a></div>'
        + (f'<div style="font-size:13px;color:#cdd0d4;margin-bottom:5px">{brief}</div>' if brief else '')
        + (f'<div style="padding:4px 8px;border-left:2px solid #30363d;margin-bottom:5px">{ev_items}</div>'
           if ev_items else '')
        + f'<div style="margin-top:4px">'
        + _badge(fund, color)
        + _badge(trade, _TRADE_COLORS.get(trade, "#57606a"))
        + '</div></div>'
    )


def _render_liveblog_row(sub: dict) -> str:
    """Render one liveblog subitem (timestamped entry from Dienos Pulsas)."""
    time_str = _esc(sub.get("time") or "")
    headline = _esc(sub.get("headline_en") or "")
    brief_lt = _esc(sub.get("brief_lt") or "")
    url = _esc(sub.get("_parent_url") or "")
    signal = (sub.get("signal") or "unclear").lower()
    entities = sub.get("entities") or []
    score = sub.get("investment_score", "")
    sig_color = _FUND_COLORS.get(signal, "#8c959f")

    ent_html = (
        f' <span style="font-size:11px;color:#8c959f">'
        + ", ".join(_esc(e) for e in entities[:3])
        + "</span>"
    ) if entities else ""

    return (
        f'<div style="padding:8px 0;border-bottom:1px solid #30363d;font-family:{_F}'
        f';display:flex;align-items:flex-start;gap:10px">'
        + (f'<span style="font-size:11px;color:#8c959f;white-space:nowrap;padding-top:2px">{time_str}</span>'
           if time_str else '')
        + f'<div>'
        + (f'<a href="{url}" style="color:#4493f8;text-decoration:none;font-size:13px;font-weight:600">'
           f'{headline}</a>'
           if url else f'<span style="font-size:13px;font-weight:600">{headline}</span>')
        + ent_html
        + (f'<div style="font-size:12px;color:#8c959f;font-style:italic;margin-top:2px">"{brief_lt}"</div>'
           if brief_lt else '')
        + f'<div style="margin-top:3px">'
        + _badge(signal, sig_color)
        + (f'<span style="font-size:10px;color:#8c959f">score:{score}</span>' if score else '')
        + '</div>'
        + '</div></div>'
    )


def _render_tickers_table(items: list[dict]) -> str:
    """One row per verified ticker from direct_public_company cards only."""
    rows = []
    seen: set[str] = set()
    for item in items:
        # Only directly-named company articles belong in this table
        if (item.get("article_type") or "") != "direct_public_company":
            continue
        fund = (item.get("signal_fundamental") or "unclear").lower()
        react = (item.get("signal_market_reaction") or "unknown").lower()
        url = _esc(item.get("url") or "")
        what = _esc((item.get("brief_bullet") or item.get("headline_en") or "")[:120])
        for p in (item.get("public_tickers") or []):
            t = p.get("ticker", "")
            role = (p.get("role") or "direct").lower()
            if not t or t in seen or role != "direct":
                continue
            seen.add(t)
            rows.append(
                f'<tr style="border-bottom:1px solid #30363d">'
                f'<td style="padding:7px 8px;font-weight:700;white-space:nowrap;color:#4493f8">'
                f'<a href="{url}" style="color:#4493f8;text-decoration:none">{_esc(t)}</a></td>'
                f'<td style="padding:7px 8px;color:#8c959f;font-size:12px;white-space:nowrap">'
                f'{_esc(p.get("exchange",""))}</td>'
                f'<td style="padding:7px 8px;font-size:13px;color:#cdd0d4">{what}</td>'
                f'<td style="padding:7px 8px;white-space:nowrap">'
                f'{_badge(fund, _FUND_COLORS.get(fund,"#57606a"))}</td>'
                f'<td style="padding:7px 8px;white-space:nowrap">'
                f'{_badge(react, _REACT_COLORS.get(react,"#8c959f"))}</td>'
                f'</tr>'
            )
    if not rows:
        return ""
    header = (
        '<tr style="border-bottom:1px solid #444c56">'
        '<th style="padding:7px 8px;text-align:left;font-size:11px;color:#8c959f">TICKER</th>'
        '<th style="padding:7px 8px;text-align:left;font-size:11px;color:#8c959f">EXCHANGE</th>'
        '<th style="padding:7px 8px;text-align:left;font-size:11px;color:#8c959f">EVENT</th>'
        '<th style="padding:7px 8px;text-align:left;font-size:11px;color:#8c959f">FUNDAMENTAL</th>'
        '<th style="padding:7px 8px;text-align:left;font-size:11px;color:#8c959f">MARKET</th>'
        '</tr>'
    )
    return (
        f'<table style="width:100%;border-collapse:collapse;font-family:{_F};'
        f'font-size:13px;max-width:680px">'
        + header + "".join(rows) + '</table>'
    )


_ASSET_CLASS_LABELS: dict[str, str] = {
    "equity_index": "Index",
    "volatility_index": "Volatility",
    "commodity": "Commodity",
    "fx": "FX",
    "rates": "Rates",
    "crypto": "Crypto",
    "etf": "ETF",
}
_DIR_COLORS = {
    "bullish": "#1a7f37", "bearish": "#cf222e",
    "mixed": "#9a6700", "neutral": "#57606a", "unclear": "#8c959f",
}


def _render_instruments_section(instruments: list[dict]) -> str:
    """Render Market Instruments mini-cards (one per resolved instrument)."""
    if not instruments:
        return ""
    parts: list[str] = []
    for instr in instruments:
        display = _esc(instr.get("display") or instr.get("symbol") or "")
        symbol = _esc(instr.get("symbol") or "")
        asset_class = instr.get("asset_class") or ""
        ac_label = _ASSET_CLASS_LABELS.get(asset_class, asset_class.replace("_", " ").title())
        direction = (instr.get("direction") or "unclear").lower()
        unit = instr.get("unit") or ""
        evidence_arts = instr.get("evidence_articles") or []

        # Collect unique evidence snippets and market_read across contributing articles
        all_evidence: list[str] = []
        seen_ev: set[str] = set()
        market_reads: list[str] = []
        affected_direct_all: list[str] = []
        affected_indirect_all: list[str] = []
        article_links: list[str] = []
        for art in evidence_arts:
            for snip in (art.get("evidence_lt") or []):
                norm = " ".join(snip.split()).lower()
                if norm not in seen_ev:
                    seen_ev.add(norm)
                    all_evidence.append(snip)
            mr = (art.get("market_read") or "").strip()
            if mr and mr not in market_reads:
                market_reads.append(mr)
            for d in (art.get("affected_direct") or []):
                if d not in affected_direct_all:
                    affected_direct_all.append(d)
            for d in (art.get("affected_indirect") or []):
                if d not in affected_indirect_all:
                    affected_indirect_all.append(d)
            url = art.get("url") or ""
            headline = art.get("headline_en") or ""
            if url and headline:
                article_links.append(
                    f'<a href="{_esc(url)}" style="color:#4493f8;text-decoration:none">'
                    f'{_esc(headline)}</a>'
                )

        ev_html = "".join(
            f'<div style="margin:2px 0">• &ldquo;{_esc(s)}&rdquo;</div>'
            for s in all_evidence[:5]
        )
        affected_parts: list[str] = []
        if affected_direct_all:
            affected_parts.append(
                f'<strong>Direct:</strong> {_join(affected_direct_all[:4])}'
            )
        if affected_indirect_all:
            affected_parts.append(
                f'<strong>Indirect:</strong> {_join(affected_indirect_all[:4])}'
            )
        affected_html = " &nbsp;|&nbsp; ".join(affected_parts) if affected_parts else ""

        parts.append(
            f'<div style="border:1px solid #444c56;border-radius:6px;padding:12px 16px;'
            f'margin:0 0 12px;font-family:{_F};max-width:680px">'
            # header row
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;'
            f'flex-wrap:wrap">'
            f'<span style="font-size:14px;font-weight:700;color:#cdd0d4">{display}</span>'
            f'<span style="font-size:11px;color:#8c959f">{symbol}</span>'
            + (f'<span style="font-size:11px;color:#8c959f">{_esc(unit)}</span>' if unit else "")
            + _badge(ac_label, "#0969da")
            + _badge(direction, _DIR_COLORS.get(direction, "#8c959f"))
            + "</div>"
            # evidence
            + (
                f'<div style="padding:6px 10px;border-left:3px solid #30363d;'
                f'color:#8c959f;font-size:12px;font-style:italic;margin-bottom:8px">'
                + ev_html + "</div>"
                if ev_html else ""
            )
            # market read (first contributing article's)
            + (
                f'<div style="font-size:13px;color:#cdd0d4;margin-bottom:8px">'
                f'<strong>Why it matters:</strong> {_esc(market_reads[0])}</div>'
                if market_reads else ""
            )
            # affected sectors / companies
            + (
                f'<div style="font-size:12px;color:#8c959f;margin-bottom:6px">'
                f'<strong>Affected:</strong> {affected_html}</div>'
                if affected_html else ""
            )
            # source article links
            + (
                f'<div style="font-size:11px;color:#8c959f">'
                + " · ".join(article_links[:2])
                + "</div>"
                if article_links else ""
            )
            + "</div>"
        )
    return "".join(parts)


def render_html(result: dict, today: dt.date) -> str:
    top_signals: list[dict] = result.get("top_signals") or []
    watchlist: list[dict] = result.get("watchlist") or []
    liveblogs: list[dict] = result.get("liveblogs") or []
    instruments: list[dict] = result.get("instruments") or []
    skipped: list[dict] = result.get("skipped") or []
    all_main = top_signals + watchlist

    wrap = f'font-family:{_F};max-width:720px;font-size:14px;line-height:1.5'
    if not all_main and not liveblogs:
        return f'<div style="{wrap}"><p>No new investing-relevant VŽ articles in this period.</p></div>'

    h2s = (f'font-family:{_F};font-size:13px;color:#8c959f;text-transform:uppercase;'
           f'letter-spacing:.6px;margin:32px 0 12px;font-weight:600;'
           f'border-bottom:1px solid #30363d;padding-bottom:6px')

    parts = [
        f'<div style="{wrap}">',
        f'<div style="font-size:12px;color:#8c959f;margin-bottom:20px;font-family:{_F}">'
        f'Investment Brief · {today.isoformat()} · {len(top_signals)} signals · '
        f'{len(watchlist)} watchlist</div>',
    ]

    # ── A · Executive Brief ───────────────────────────────────────────
    # Source: top 6 highest-scoring items across Top Signals + Watchlist + important liveblogs.
    brief_pool: list[dict] = sorted(
        top_signals + watchlist,
        key=lambda x: -x.get("investment_score", 0),
    )
    # Also include high-scoring liveblog subitems (score >= _SCORE_BRIEF)
    lb_brief = [s for s in liveblogs if s.get("investment_score", 0) >= _SCORE_BRIEF]
    lb_brief_sorted = sorted(lb_brief, key=lambda x: -x.get("investment_score", 0))

    bullets = []
    used_lb = 0
    for item in brief_pool[:6]:
        b = (item.get("brief_bullet") or "").strip()
        if not b:
            fallback = (item.get("market_read") or item.get("headline_en") or "").strip()
            words = fallback.split()[:22]
            b = " ".join(words)
            if not b.endswith("."):
                b = b.rstrip(",;") + "."
        bullets.append(b)
    # Fill up to 6 from important liveblogs if brief_pool produced fewer
    for sub in lb_brief_sorted:
        if len(bullets) >= 6:
            break
        h = (sub.get("headline_en") or "").strip()
        if h:
            bullets.append(h if h.endswith(".") else h + ".")

    if bullets:
        parts.append(f'<h2 style="{h2s}">A · Executive Brief</h2>')
        parts.append(
            f'<div style="border:1px solid #444c56;border-left:4px solid #4493f8;'
            f'border-radius:8px;padding:14px 18px;margin:0 0 20px;font-family:{_F}">'
            f'<ul style="margin:0;padding-left:20px">'
        )
        parts.extend(
            f'<li style="margin:9px 0;font-size:14px;line-height:1.5">{_esc(b)}</li>'
            for b in bullets
        )
        parts.append('</ul></div>')

    # ── B · Top Signals ───────────────────────────────────────────────
    if top_signals:
        parts.append(f'<h2 style="{h2s}">B · Top Signals</h2>')
        parts.extend(_render_card(c) for c in top_signals)

    # ── C · Direct Public Tickers ─────────────────────────────────────
    ticker_table = _render_tickers_table(top_signals + watchlist)
    if ticker_table:
        parts.append(f'<h2 style="{h2s}">C · Direct Public Tickers</h2>')
        parts.append(ticker_table)

    # ── C2 · Market Instruments ───────────────────────────────────────
    instr_html = _render_instruments_section(instruments)
    if instr_html:
        parts.append(f'<h2 style="{h2s}">C2 · Market Instruments</h2>')
        parts.append(instr_html)

    # ── D · Macro / Sector / Private Watchlist ────────────────────────
    if watchlist:
        parts.append(f'<h2 style="{h2s}">D · Macro / Sector / Private Watchlist</h2>')
        parts.extend(_render_watchlist_row(i) for i in watchlist)

    # ── E · Dienos Pulsas / Live Market Updates ──────────────────────
    if liveblogs:
        parts.append(f'<h2 style="{h2s}">E · Dienos Pulsas / Live Market Updates</h2>')
        parts.append(f'<div style="font-family:{_F}">')
        parts.extend(_render_liveblog_row(s) for s in liveblogs)
        parts.append('</div>')

    # ── F · Skipped / Low Relevance ───────────────────────────────────
    if skipped:
        parts.append(f'<h2 style="{h2s}">F · Skipped / Low Relevance</h2>')
        parts.append(
            f'<div style="font-family:{_F};font-size:12px;color:#8c959f;'
            f'padding:10px 14px;border:1px solid #30363d;border-radius:6px">'
        )
        for s in skipped:
            parts.append(
                f'<div style="margin:3px 0">— <em>{_esc(s.get("headline_en","—"))}</em>'
                f' · {_esc(s.get("reason",""))}</div>'
            )
        parts.append('</div>')

    parts.append('</div>')
    return "".join(parts)


# Login + scrape
def login_and_fetch(items: list[dict]) -> list[dict]:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="lt-LT",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"),
        )
        # Block heavy assets to speed scraping
        ctx.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,mp4,woff,woff2,ico}",
            lambda r: r.abort(),
        )
        page = ctx.new_page()

        # Login
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        page.fill("#email", VZ_EMAIL)
        page.click('button:has-text("Prisijungti")')
        page.wait_for_selector('input[type="password"]', timeout=15000)
        page.fill('input[type="password"]', VZ_PASSWORD)
        page.click('button:has-text("Prisijungti")')
        page.wait_for_url(lambda u: "slaptazodis" not in u, timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        # Verify login
        page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=20000)
        if "Atsijungti" not in page.content():
            raise RuntimeError("Login failed: no 'Atsijungti' marker on homepage after login.")

        # Fetch articles
        for item in items:
            try:
                page.goto(item["url"], wait_until="domcontentloaded",
                          timeout=ARTICLE_FETCH_TIMEOUT_MS)
                html = page.content()
                text = trafilatura.extract(
                    html,
                    include_links=False,
                    include_tables=True,
                    favor_recall=True,
                ) or ""
                if len(text) < 300:
                    print(f"  skip (extracted text <300 chars): {item['url']}")
                    continue
                if any(m in text for m in PAYWALL_TEXT_MARKERS):
                    print(f"  skip (paywall in extracted text): {item['url']}")
                    continue
                item["body"] = text
                out.append(item)
                print(f"  fetched ({len(text)} chars): {item['url']}")
            except Exception as e:
                print(f"  fetch FAILED {item['url']}: {e}")
                continue

        browser.close()
    return out


# Email
def _build_attachments(top_signals: list[dict], fetched: list[dict]) -> list[dict]:
    """Build .txt attachments for each Top Signal article."""
    body_by_url = {a["url"]: a.get("body", "") for a in fetched}
    attachments = []
    for idx, item in enumerate(top_signals, 1):
        url = item.get("url", "")
        title = item.get("headline_en") or item.get("title") or f"article-{idx}"
        body = body_by_url.get(url, "")
        if not body:
            continue
        # Safe filename: keep alphanumeric + spaces, collapse, trim
        safe = re.sub(r"[^\w\s-]", "", title)
        safe = re.sub(r"\s+", "_", safe.strip())[:60]
        filename = f"{idx:02d}_{safe}.txt"
        content = f"TITLE: {title}\nURL:   {url}\n\n{'=' * 60}\n\n{body}\n"
        attachments.append({"filename": filename, "content": content})
    return attachments


def send_email(subject: str, html_body: str,
               attachments: list[dict] | None = None) -> None:
    """Send HTML email with optional .txt attachments.

    Each entry in `attachments` is {"filename": str, "content": str}.
    Uses explicit MIME classes so attachments land in multipart/mixed,
    not buried inside multipart/alternative.
    """
    if attachments:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = SMTP_TO
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText("This message requires an HTML-capable email client.", "plain"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
        for att in attachments:
            part = MIMEText(att["content"], "plain", "utf-8")
            part.add_header("Content-Disposition", "attachment",
                            filename=att["filename"])
            msg.attach(part)
    else:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = SMTP_TO
        msg.set_content("This message requires an HTML-capable email client.")
        msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


# Main
def run() -> None:
    now = dt.datetime.now(tz=dt.timezone.utc)
    last_run = load_last_run()
    print(f"Last run: {last_run.isoformat()}")
    print(f"This run: {now.isoformat()}")

    rss_items = fetch_rss(since=last_run)
    print(f"RSS items since last run: {len(rss_items)}")

    high, conditional, _skip = tier_filter(rss_items)
    print(f"High-tier: {len(high)} | Conditional: {len(conditional)}")

    # Note: conditional articles flow straight into extraction; the Gemini
    # extraction prompt classifies and discards low-relevance items.

    candidates = (high + conditional)
    candidates.sort(key=lambda x: x["published"], reverse=True)
    candidates = candidates[:MAX_ARTICLES]
    print(f"Selected {len(candidates)} articles for full extraction")

    if not candidates:
        send_email(
            f"VŽ summary {now.date().isoformat()} — no new articles",
            "<p>No new investing-relevant VŽ articles since last run.</p>" + DISCLAIMER_HTML,
        )
        save_last_run(now)
        return

    fetched = login_and_fetch(candidates)
    print(f"Fetched {len(fetched)} article bodies")

    if not fetched:
        send_email(
            f"VŽ summary {now.date().isoformat()} — fetch issues",
            "<p>No article bodies could be fetched. Login may have failed or "
            "all candidates were paywalled in the extracted text.</p>" + DISCLAIMER_HTML,
        )
        save_last_run(now)
        return

    extracted = gemini_extract(fetched)
    result = validate(extracted, fetched)
    n_top = len(result["top_signals"])
    n_watch = len(result["watchlist"])
    n_lb = len(result["liveblogs"])
    n_skip = len(result["skipped"])
    top_scores = [i.get("investment_score", 0) for i in result["top_signals"]]
    watch_scores = [i.get("investment_score", 0) for i in result["watchlist"]]
    print(f"Extracted {len(extracted.get('items', []))} | "
          f"Top {n_top} scores={top_scores} | Watchlist {n_watch} scores={watch_scores} | "
          f"Liveblogs {n_lb} | Skipped {n_skip}")

    html = render_html(result, now.date())
    attachments = _build_attachments(result["top_signals"], fetched)
    print(f"Attaching {len(attachments)} article(s)")
    send_email(
        f"VŽ summary {now.date().isoformat()} — {n_top} signals · {n_watch} watchlist",
        html + DISCLAIMER_HTML,
        attachments=attachments,
    )
    save_last_run(now)
    print("Done.")


def main() -> None:
    try:
        run()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            today = dt.datetime.now(tz=dt.timezone.utc).date().isoformat()
            send_email(
                f"VŽ summary FAILED {today}",
                f"<h3>Pipeline failure</h3><pre style='background:#f6f8fa;"
                f"padding:12px;border-radius:6px;overflow:auto'>{tb}</pre>",
            )
        except Exception as e2:
            print(f"Failure email itself failed: {e2}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
