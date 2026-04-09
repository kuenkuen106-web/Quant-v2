# =============================================================================
# ⚙️ UAT 時光機模式 (The Ultimate Edition - Full Integration)
# 核心功能：模擬過去交易日 / 波段與短線雙引擎 / 大盤 FTD 偵測 / 部位計算機
# =============================================================================

import pandas as pd, numpy as np, yfinance as yf, matplotlib
matplotlib.use('Agg') # 伺服器端繪圖必須加上這行
import matplotlib.pyplot as plt, matplotlib.dates as mdates, concurrent.futures
import warnings, os, datetime, json, logging, time, requests
from io import StringIO
from fake_useragent import UserAgent

# 關閉不必要嘅警告，保持 Terminal 乾淨
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')
plt.style.use('dark_background')
plt.ioff()

# =============================================================================
# 系統環境設定 (路徑與 Webhook - UAT 專用)
# =============================================================================
# 寫入 UAT 子資料夾，避免覆蓋正式版
OUTPUT_DIR = "docs/UAT"
CHARTS_DIR = os.path.join(OUTPUT_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# 讀取 GitHub Secrets (UAT 專用的 Webhook)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_BACKTEST_WEBHOOK_URL", "")
DISCORD_SUMMARY_WEBHOOK = os.environ.get("DISCORD_BACKTEST_SUMMARY_WEBHOOK", "")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "uat_trade_history.json")

# =============================================================================
# 核心策略與時光機參數 
# =============================================================================
LOOKBACK_YEARS = 3
PQR_SWING_MIN = 75
FTD_VALID_DAYS = 20
MAX_ACCOUNT_RISK_PCT = 0.01 # 每單最多虧損總資金的 1%

# 👇 時光機設定：從 GitHub Actions 讀取要回溯幾多日 (預設回溯 10 日)
raw_days = os.environ.get("UAT_DAYS_AGO", "10")
SIMULATE_DAYS_AGO = int(raw_days)

# =============================================================================
# 功能函數區
# =============================================================================
def send_discord_alert(ticker, strategy_name, price, sl, tp, is_bullish, sources):
    if not DISCORD_WEBHOOK_URL: return
    unit = "¥" if ticker.endswith(".T") else "$"
    source_str = " | ".join(sources) if sources else "動態掃描"
    color = 65280 if is_bullish else 16711680 
    
    embed_data = {
        "title": f"🚨 [UAT 模擬] 系統異動觸發: {ticker}",
        "description": f"**{strategy_name}** 條件已達成！\n🔍 來源: `{source_str}`",
        "color": color,
        "fields": [
            {"name": "💵 模擬當時價格", "value": f"{unit}{price}", "inline": True},
            {"name": "🛑 建議止損", "value": f"{unit}{sl}", "inline": True},
            {"name": "🎯 建議止盈", "value": f"{unit}{tp}", "inline": True}
        ],
        "footer": {"text": f"時光機模式執行中 | 模擬日期: {today_str}"}
    }
    try: 
        res = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed_data]})
        if res.status_code == 429: print(f"⚠️ Discord 拒絕接收 - 傳送太快！")
        time.sleep(0.5) 
    except Exception as e: print(f"⚠️ Discord 連線錯誤: {e}")

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

trade_history = load_history()

# =============================================================================
# MODULE 1 & 2 — 雙市場數據引擎與時光機截斷
# =============================================================================
print(f"⏳ [1-3/7] 正在抓取數據與啟動時光機 (回溯 {SIMULATE_DAYS_AGO} 日)...")

def build_dynamic_watchlist():
    ticker_sources = {}
    ua = UserAgent()

    def add_to_map(tickers, source_label):
        for t in tickers:
            if not isinstance(t, str) or len(t) < 1: continue
            clean_t = t.strip()
            if not clean_t.endswith('.T'): clean_t = clean_t.replace('.', '-')
            if clean_t not in ticker_sources: ticker_sources[clean_t] = []
            if source_label not in ticker_sources[clean_t]: ticker_sources[clean_t].append(source_label)
    
    # ---------------------------------------------------------
    # 1. 🇺🇸 美股黃金板塊擴充 (超過 1500 隻)
    # ---------------------------------------------------------
    try:
        wiki_us_indexes = [
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "S&P500_大盤"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "S&P400_中型"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "S&P600_小型"),
        ("https://en.wikipedia.org/wiki/Nasdaq-100", "NDX100_科技")]

        for url, label in wiki_us_indexes:
            res = requests.get(url, headers={'User-Agent': ua.random}, timeout=10)
            tables = pd.read_html(StringIO(res.text))
            
            # 自動尋找包含 Symbol 或 Ticker 的表格
            for df in tables:
                target_col = next((col for col in df.columns if 'symbol' in str(col).lower() or 'ticker' in str(col).lower()), None)
                if target_col:
                    add_to_map(df[target_col].dropna().astype(str).tolist(), label)
                    print(f"  ✅ 成功載入 {label}: {len(df)} 隻")
                    break
       
        #csv_url = "https://raw.githubusercontent.com/datasets/s-p-500-companies/master/data/constituents.csv"
        #df_sp = pd.read_csv(csv_url, timeout=10)
        #add_to_map(df_sp['Symbol'].tolist(), "S&P500")
    except:
        print(f"  ⚠️ S&P 500 CSV 載入失敗，啟動超級後備名單: {e}")
        # 超強後備名單 (超過 400 隻美股核心成分股)
        sp500_fallback = [
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "BRK-B", "TSLA", "UNH",
            "JPM", "XOM", "V", "MA", "AVGO", "PG", "HD", "JNJ", "LLY", "COST",
            "CVX", "MRK", "ABBV", "PEP", "KO", "TMO", "PFE", "BAC", "ORCL", "MCD",
            "CSCO", "CRM", "ABT", "ACN", "LIN", "NFLX", "AMD", "DIS", "WMT", "TXN",
            "DHR", "PM", "NKE", "NEE", "VZ", "RTX", "UPS", "HON", "QCOM", "AMGN",
            "LOW", "SPGI", "IBM", "INTU", "CAT", "UNP", "COP", "SBUX", "DE", "GS",
            "PLD", "MS", "BLK", "ELV", "GILD", "ISRG", "TJX", "LMT", "SYK", "ADP",
            "MDT", "VRTX", "MMC", "AMT", "GE", "CI", "CB", "NOW", "ADI", "LRCX",
            "MDLZ", "T", "ETN", "REGN", "ZTS", "BSX", "MU", "PANW", "PGR", "FI",
            "SNPS", "C", "KLAC", "VLO", "CDNS", "WM", "EOG", "SHW", "MAR", "MCK",
            "CVS", "MO", "PH", "GD", "ORLY", "APH", "SLB", "ITW", "USB", "FDX",
            "ECL", "ROP", "PXD", "TGT", "BDX", "NXPI", "CMG", "MNST", "MPC", "MCO",
            "CTAS", "AIG", "NSC", "PSX", "ADSK", "AON", "EMR", "MET", "D", "KMB",
            "SRE", "MSI", "MCHP", "AJG", "HCA", "AZO", "F", "WELL", "EW", "DRE",
            "O", "PCAR", "GPN", "ADP", "FIS", "HUM", "PAYX", "TEL", "DOW", "BKR",
            "ADM", "KDP", "STZ", "CNC", "JCI", "SYY", "CTSH", "CARR", "DXCM", "EIX",
            "IDXX", "VRSK", "DLR", "IQV", "A", "GWW", "COR", "ED", "NEM", "CHTR",
            "YUM", "OXY", "MSCI", "KHC", "WFC", "TFC", "PNC", "COF", "DFS", "SYF",
            "KEY", "RF", "HBAN", "FITB", "CFG", "STT", "NTRS", "MTB", "BK", "AMP",
            "IVZ", "BEN", "TROW", "GL", "L", "AIZ", "RE", "TRV", "CBRE", "HST",
            "SPG", "AVB", "EQR", "VTR", "PEAK", "BXP", "MAA", "CPT", "UDR", "ESS",
            "ARE", "VICI", "PSA", "EXR", "SBAC", "CCI", "AWK", "NI", "PNW", "ATO",
            "LNT", "ES", "WEC", "CMS", "XEL", "ETR", "FE", "AEE", "AEP", "PEG",
            "DTE", "PPL", "DUK", "SO", "CNP", "VST", "PARA", "WBD", "NWSA", "NWS",
            "FOXA", "FOX", "LYV", "MTCH", "EA", "TTWO", "OMC", "IPG", "TMUS", "LUMN",
            "FYBR", "AMX", "ROST", "HLT", "DHI", "LEN", "PHM", "NVR", "GRMN", "GM",
            "BBY", "EBAY", "ETSY", "RVTY", "POOL", "HAS", "MAT", "EL", "CL", "K",
            "GIS", "CPB", "HRL", "SJM", "TAP", "KR", "WBA", "DLTR", "DG", "HAL",
            "HES", "DVN", "FANG", "MRO", "APA", "CTRA", "OKE", "TRGP", "KMI", "WMB",
            "SCHW", "RJF", "LPLA", "AXP", "PYPL", "FISV", "JKHY", "WTW", "PRU", "AFL",
            "ALL", "HIG", "CINF", "NDAQ", "CME", "ICE", "BMY", "STE", "WAT", "MTD",
            "CRL", "RMD", "BA", "NOC", "TDG", "HWM", "TXT", "MMM", "AME", "ROK",
            "DOV", "XYL", "FAST", "RSG", "CSX", "INVH", "AMH", "EQIX", "INTC", "AMAT",
            "ANSS", "SAP", "FTNT", "STX", "WDC", "HPQ", "DELL", "NTAP"
        ]
        add_to_map(sp500_fallback, "S&P500")
        print(f"  ✅ 成功載入 S&P 500 後備名單 (共 {len(sp500_fallback)} 隻)")
    # ---------------------------------------------------------
    # 2. 獲取 Finviz 異動股 (Unusual Volume & Top Gainers)
    # ---------------------------------------------------------
    # 呢度係捕捉「當日最熱門」標的關鍵
    finviz_urls = [
        ("https://finviz.com/screener.ashx?v=111&s=ta_topgainers", "Finviz升幅"),
        ("https://finviz.com/screener.ashx?v=111&s=ta_unusualvolume", "Finviz異動")
    ]
    for url, label in finviz_urls:
        try:
            # 每次需要 headers 時，呼叫 ua.random
            headers = {'User-Agent': ua.random}
            res = requests.get(url, headers=headers, timeout=10)
            tables = pd.read_html(res.text)
            # Finviz 的股票代號通常在最後幾個表格中，且長度為 1-5 字符
            for df in tables[-3:]: 
                if 1 in df.columns:
                    found = [str(t) for t in df[1].tolist() if str(t).isupper() and 1 <= len(str(t)) <= 5]
                    if found:
                        add_to_map(found, label)
                        print(f"  🔥 捕捉到 {label}: {len(found)} 隻")
                        break
        except:
            print(f"  ⚠️ {label} 抓取略過")

    # ---------------------------------------------------------
    # 3. 獲取日股動態名單 (Nikkei 225 + 當日熱門)
    # ---------------------------------------------------------
    wiki_jp_indexes = [
        ("https://en.wikipedia.org/wiki/Nikkei_225", "NK225"),
        ("https://en.wikipedia.org/wiki/TOPIX_100", "TOPIX100"),
        ("https://ja.wikipedia.org/wiki/TOPIX_Mid400", "TOPIX_Mid400_中型"),
        ("https://ja.wikipedia.org/wiki/TOPIX_Small500", "TOPIX_Small500_小型")
    ]

    try:
        for url, label in wiki_jp_indexes:
            try:
                res = requests.get(url, headers={'User-Agent': ua.random}, timeout=10)
                tables = pd.read_html(StringIO(res.text))
                    
                import re
                target_col = None
                # 自動尋找包含最多股票代號嘅表格 (日股通常係 4 位數字)
                target_table = max(tables, key=len)
                    
                for col in target_table.columns:
                    col_name = str(col).lower()
                    if 'code' in col_name or 'ticker' in col_name or 'symbol' in col_name or 'コード' in col_name:
                        target_col = col; break
                    
                if target_col is None:
                    for col in target_table.columns:
                        sample_vals = target_table[col].dropna().astype(str).tolist()[:5]
                        if sample_vals and all(re.match(r'^\d{4}$', str(x)) for x in sample_vals):
                            target_col = col; break

                if target_col is not None:
                    found_nk = [f"{str(x)}.T" for x in target_table[target_col] if re.match(r'^\d{4}$', str(x))]
                    add_to_map(list(dict.fromkeys(found_nk)), label)
                    print(f"  ✅ 成功從 Wikipedia 載入 {label} (共 {len(found_nk)} 隻)")
            except Exception as e:
                print(f"  ⚠️ {label} 載入失敗: {e}")
    except Exception as e:
            print(f"  ⚠️ 日股名單載入失敗: {e}")
            # 如果 fail, 手動加入2026/04/05 list
            nk225_tickers = [
            "1332.T", "1605.T", "1721.T", "1801.T", "1802.T", "1803.T", "1812.T", "1925.T", "1928.T", "1963.T",
            "2002.T", "2267.T", "2282.T", "2413.T", "2432.T", "2501.T", "2502.T", "2503.T", "2531.T", "2768.T",
            "2801.T", "2802.T", "2871.T", "2914.T", "3086.T", "3099.T", "3101.T", "3103.T", "3289.T", "3382.T",
            "3401.T", "3402.T", "3405.T", "3407.T", "3436.T", "3659.T", "3861.T", "3863.T", "4004.T", "4005.T",
            "4021.T", "4042.T", "4043.T", "4061.T", "4063.T", "4151.T", "4183.T", "4188.T", "4208.T", "4324.T",
            "4452.T", "4502.T", "4503.T", "4506.T", "4507.T", "4519.T", "4523.T", "4543.T", "4568.T", "4578.T",
            "4661.T", "4689.T", "4704.T", "4751.T", "4755.T", "4901.T", "4911.T", "5019.T", "5020.T", "5101.T",
            "5108.T", "5201.T", "5202.T", "5214.T", "5232.T", "5233.T", "5301.T", "5332.T", "5333.T", "5401.T",
            "5406.T", "5411.T", "5541.T", "5631.T", "5703.T", "5706.T", "5707.T", "5711.T", "5713.T", "5801.T",
            "5802.T", "5803.T", "5901.T", "6098.T", "6103.T", "6113.T", "6178.T", "6301.T", "6302.T", "6305.T",
            "6326.T", "6361.T", "6367.T", "6471.T", "6472.T", "6473.T", "6501.T", "6503.T", "6504.T", "6506.T",
            "6645.T", "6674.T", "6701.T", "6702.T", "6703.T", "6723.T", "6724.T", "6752.T", "6753.T", "6758.T",
            "6762.T", "6770.T", "6841.T", "6857.T", "6902.T", "6920.T", "6952.T", "6954.T", "6971.T", "6976.T",
            "6981.T", "6988.T", "7011.T", "7012.T", "7013.T", "7186.T", "7201.T", "7202.T", "7203.T", "7205.T",
            "7211.T", "7261.T", "7267.T", "7269.T", "7270.T", "7272.T", "7731.T", "7733.T", "7735.T", "7741.T",
            "7751.T", "7752.T", "7832.T", "7911.T", "7912.T", "7951.T", "8001.T", "8002.T", "8015.T", "8031.T",
            "8035.T", "8053.T", "8058.T", "8233.T", "8252.T", "8253.T", "8267.T", "8304.T", "8306.T", "8308.T",
            "8309.T", "8316.T", "8331.T", "8354.T", "8411.T", "8601.T", "8604.T", "8628.T", "8630.T", "8697.T",
            "8725.T", "8750.T", "8766.T", "8795.T", "8801.T", "8802.T", "8804.T", "8830.T", "9001.T", "9005.T",
            "9007.T", "9008.T", "9009.T", "9020.T", "9021.T", "9022.T", "9041.T", "9042.T", "9062.T", "9064.T",
            "9101.T", "9104.T", "9107.T", "9201.T", "9202.T", "9301.T", "9412.T", "9432.T", "9433.T", "9434.T",
            "9501.T", "9502.T", "9503.T", "9531.T", "9532.T", "9602.T", "9613.T", "9681.T", "9735.T", "9766.T",
            "9843.T", "9983.T", "9984.T"
            ]
            # 執行合併
            add_to_map(nk225_tickers, "NK225")

    # B. 捕捉 JP Trending (保持不變)
    try:
        jp_trending_url = "https://query1.finance.yahoo.com/v1/finance/trending/JP?count=20"
        # 每次需要 headers 時，呼叫 ua.random
        headers = {'User-Agent': ua.random}
        res_jp = requests.get(jp_trending_url, headers=headers, timeout=5)
        # 加入 len 檢查，防止 list index out of range
        if res_jp.status_code == 200 and len(res_jp.json().get('finance', {}).get('result', [])) > 0:
            jp_trending = [q['symbol'] for q in res_jp.json()['finance']['result'][0]['quotes']]
            add_to_map(jp_trending, "JP熱門")
            print(f"  🔥 捕捉到日股當日焦點: {len(jp_trending)} 隻")
    except Exception as e:
        print(f"  ⚠️ JP Trending 略過: API 未返回數據")

    add_to_map(['SPY', '^VIX', '^N225'], "基準指數")
    return ticker_sources

TICKER_MAP = build_dynamic_watchlist()
ALL_TICKERS = list(TICKER_MAP.keys())

data_raw = yf.download(ALL_TICKERS, period=f"{LOOKBACK_YEARS}y", progress=False, threads=True, timeout=30, group_by='column')
if isinstance(data_raw.columns, pd.MultiIndex):
    closes, highs, lows, vols, opens = data_raw['Close'].ffill(), data_raw['High'].ffill(), data_raw['Low'].ffill(), data_raw['Volume'].ffill(), data_raw['Open'].ffill()
else:
    closes = data_raw[['Close']].ffill(); highs = data_raw[['High']].ffill(); lows = data_raw[['Low']].ffill(); vols = data_raw[['Volume']].ffill(); opens = data_raw[['Open']].ffill()

# ---------------------------------------------------------------------
# 🕒 【時光機關鍵邏輯】抹除「未來」數據
# ---------------------------------------------------------------------
if SIMULATE_DAYS_AGO > 0:
    print(f"⏰ [時光機] 正在抹除最近 {SIMULATE_DAYS_AGO} 天數據，回溯中...")
    closes = closes.iloc[:-SIMULATE_DAYS_AGO]
    highs = highs.iloc[:-SIMULATE_DAYS_AGO]
    lows = lows.iloc[:-SIMULATE_DAYS_AGO]
    vols = vols.iloc[:-SIMULATE_DAYS_AGO]
    opens = opens.iloc[:-SIMULATE_DAYS_AGO]
# ---------------------------------------------------------------------

# 👇 絕對唔可以用 datetime.now()！必須用截斷後 DataFrame 嘅最後一日！
today_str = closes.index[-1].strftime('%Y-%m-%d')
print(f"📅 [UAT] 模擬今日日期：{today_str}")

# =============================================================================
# MODULE 3 — 雙市場宏觀剖析 (FTD, 市寬, 派發日 獨立計算)
# =============================================================================
vix_c = closes['^VIX'].ffill()

jp_tickers = [t for t in closes.columns if str(t).endswith('.T')]
us_tickers = [t for t in closes.columns if not str(t).endswith('.T') and t not in ['SPY', '^VIX', '^N225']]

def calc_breadth(ticker_list):
    if not ticker_list: return 0
    sub_closes = closes[ticker_list]
    breadth = (sub_closes > sub_closes.rolling(50).mean()).sum(axis=1) / sub_closes.shape[1] * 100
    return round(float(breadth.iloc[-1]), 1)

us_breadth = calc_breadth(us_tickers)
jp_breadth = calc_breadth(jp_tickers)

def calc_macro_regime(index_ticker):
    idx_c, idx_v, idx_l = closes[index_ticker], vols[index_ticker], lows[index_ticker]
    ret = idx_c.pct_change()
    dist_mask = (ret < -0.002) & (idx_v > idx_v.shift(1))
    curr_dist_days = int(dist_mask.rolling(25).sum().iloc[-1])
    
    ftd_history = np.zeros(len(idx_c))
    rally_day, rally_low, last_ftd_idx = 0, float('inf'), -999
    
    for i in range(1, len(idx_c)):
        c, pc, l, v, pv = idx_c.iloc[i], idx_c.iloc[i-1], idx_l.iloc[i], idx_v.iloc[i], idx_v.iloc[i-1]
        if l < rally_low: rally_low, rally_day = l, 1 if c > pc else 0
        else:
            if c > pc: rally_day = max(1, rally_day + 1)
            elif rally_day > 0: rally_day += 1
        if rally_day >= 4 and c > pc * 1.012 and v > pv:
            last_ftd_idx, rally_low, rally_day = i, c, 0
        ftd_history[i] = (i - last_ftd_idx) if last_ftd_idx > 0 else 999
        
    curr_ftd_days = int(ftd_history[-1])
    is_bull = float(idx_c.iloc[-1]) > float(idx_c.rolling(200).mean().iloc[-1])
    
    if vix_c.iloc[-1] > 25: status, color = "🚨 VIX 恐慌警戒", "text-red-500 bg-red-500/20 border-red-500/50"
    elif is_bull: status, color = "🟢 牛市格局", "text-emerald-500 bg-emerald-500/10 border-emerald-500/20"
    elif curr_ftd_days <= FTD_VALID_DAYS: status, color = f"✅ 底部確認 ({curr_ftd_days}日 FTD)", "text-blue-400 bg-blue-500/10 border-blue-500/20"
    else: status, color = "❌ 熊市空頭", "text-red-500 bg-red-500/10 border-red-500/20"
    
    return curr_dist_days, is_bull, status, color

us_dist, us_is_bull, us_status, us_color = calc_macro_regime('SPY')
jp_dist, jp_is_bull, jp_status, jp_color = calc_macro_regime('^N225')

# 為 UAT 繪製 SPY 圖表
spy_c, spy_v, spy_l = closes['SPY'], vols['SPY'], lows['SPY']
spy_20, spy_50, spy_200 = spy_c.rolling(20).mean(), spy_c.rolling(50).mean(), spy_c.rolling(200).mean()
fig, ax = plt.subplots(figsize=(8, 3), dpi=100)
ax.plot(spy_c.index[-200:], spy_c.iloc[-200:], color='#cbd5e1', label='SPX', linewidth=1.5)
ax.plot(spy_20.index[-200:], spy_20.iloc[-200:], color='#3b82f6', label='20MA', linewidth=1, alpha=0.8)
ax.plot(spy_50.index[-200:], spy_50.iloc[-200:], color='#f59e0b', label='50MA', linewidth=1, alpha=0.8)
ax.plot(spy_200.index[-200:], spy_200.iloc[-200:], color='#dc2626', label='200MA', linestyle='-.', linewidth=1.5)
fig.patch.set_facecolor('#0f172a'); ax.set_facecolor('#0f172a')
ax.tick_params(colors='white', labelsize=8)
ax.legend(facecolor='#1e293b', labelcolor='white', loc='upper left', ncol=3, fontsize=8)
for spine in ax.spines.values(): spine.set_edgecolor('#334155')
plt.tight_layout()
plt.savefig(os.path.join(CHARTS_DIR, "SPY_Trend.png"), transparent=True)
plt.close(fig)

r126 = closes / closes.shift(126) - 1
r252 = closes / closes.shift(252) - 1
rs_rank = ((0.6 * r126) + (0.4 * r252)).rank(axis=1, pct=True) * 99 + 1
rs_momentum = rs_rank - rs_rank.shift(20)

# =============================================================================
# MODULE 4 & 5 — 雙策略判定引擎與自動結算
# =============================================================================
print(f"⏳ [4-6/7] 正在按 {today_str} 視角進行策略演算...")

current_prices = closes.iloc[-1].to_dict()
closed_this_run = []
for trade in trade_history:
    if trade.get('status') == 'OPEN':
        tk = trade.get('tk')
        if tk in current_prices and not pd.isna(current_prices[tk]):
            now_px = round(float(current_prices[tk]), 2)
            trade['last_px'] = now_px
            tp, sl = trade.get('tp'), trade.get('sl')
            if tp and now_px >= tp:
                trade['status'], trade['close_date'] = '✅ TAKE PROFIT', today_str
                closed_this_run.append(trade)
            elif sl and now_px <= sl:
                trade['status'], trade['close_date'] = '❌ STOP LOSS', today_str
                closed_this_run.append(trade)

swing_results, short_term_results, js_payload = [], [], []

for ticker in [t for t in ALL_TICKERS if t not in ['SPY','^VIX','^N225']]:
    try:
        c_raw = closes[ticker].dropna()
        if len(c_raw) < 252 + 200: continue
        c, h, l, v, op = closes[ticker], highs[ticker], lows[ticker], vols[ticker], opens[ticker]
        cp = float(c.iloc[-1])
        if (c.tail(20) * v.tail(20)).mean() < (300_000_000 if ticker.endswith('.T') else 5_000_000): continue

        is_jp = ticker.endswith('.T')
        ticker_is_bull = jp_is_bull if is_jp else us_is_bull

        rs = rs_rank[ticker].iloc[-1]
        rs_mom = rs_momentum[ticker].iloc[-1]
        
        sma20, std20 = c.rolling(20).mean(), c.rolling(20).std()
        bb_lower, bb_width = sma20 - (2 * std20), (4 * std20) / sma20
        atr = (h-l).rolling(14).mean(); catr = float(atr.iloc[-1])
        
        delta = c.diff()
        rsi = 100 - (100 / (1 + (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()))
        
        # 👇 【新增】：每日更新「目前持倉」的現時指標 (curr_metric)
        for t in trade_history:
            if t.get('status') == 'OPEN' and t.get('tk') == ticker:
                if '超賣' in t.get('tag', ''):
                    t['curr_metric'] = f"RSI: {int(rsi.iloc[-1])}"
                else:
                    t['curr_metric'] = f"RS: {int(rs)}"

        if pd.isna(rs) or rs < PQR_SWING_MIN: continue

        base_dd = (c.rolling(60).max() - c.rolling(60).min()) / c.rolling(60).max()
        rec_volat = (c.rolling(10).max() - c.rolling(10).min()) / c.rolling(10).max()
        is_vcp = (base_dd.iloc[-1] <= 0.35) and (rec_volat.iloc[-1] <= 0.06) and (v.iloc[-1] < v.rolling(50).mean().iloc[-1])
        is_bb_sqz = (bb_width.iloc[-1] <= bb_width.rolling(120).min().iloc[-1] * 1.1)

        trade_info = None 
        tag_name = ""
        sl_p, tp_p = 0, 0
        risk_per_share = 0
        entry_metric = "" # 準備記錄進場指標

        if (is_vcp or is_bb_sqz) and ticker_is_bull:
            tag_name = "🏆 VCP 突破" if is_vcp else "💥 BB 擠壓"
            sl_p, tp_p = round(cp - 2.5 * catr, 2), round(cp + 4.5 * catr, 2)
            risk_per_share = cp - sl_p
            entry_metric = f"RS: {int(rs)}" # 👈 記錄進場 RS
            
            swing_results.append({'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name})
            trade_info = {'date': today_str, 'tk': ticker, 'px': round(cp, 2), 'sl': sl_p, 'tp': tp_p, 'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name, 'entry_metric': entry_metric, 'curr_metric': entry_metric}
        
        elif not trade_info: 
            is_gap_up = ((op.iloc[-1] - c.iloc[-2]) / c.iloc[-2] >= 0.03) and (v.iloc[-1] > v.rolling(20).mean().iloc[-1] * 2)
            is_oversold = (rsi.iloc[-1] < 28) and (cp < bb_lower.iloc[-1])
            if is_gap_up or is_oversold:
                tag_name = "⚡ 缺口動能" if is_gap_up else "📉 極度超賣"
                sl_p, tp_p = round(cp * 0.95, 2), round(cp * 1.05, 2)
                risk_per_share = cp - sl_p
                entry_metric = f"RSI: {int(rsi.iloc[-1])}" if is_oversold else f"RS: {int(rs)}" # 👈 超賣記 RSI，缺口記 RS
                
                short_term_results.append({'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name})
                trade_info = {'date': today_str, 'tk': ticker, 'px': round(cp, 2), 'sl': sl_p, 'tp': tp_p, 'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name, 'entry_metric': entry_metric, 'curr_metric': entry_metric}

        if trade_info:
            send_discord_alert(ticker, tag_name, round(cp, 2), sl_p, tp_p, True, [])
            if not any(t.get('tk') == ticker and t.get('status') == 'OPEN' for t in trade_history):
                 trade_history.append(trade_info)
            
            js_payload.append({
                "ticker": ticker, "tag": tag_name, "curr_price": round(cp, 2), 
                "sl_price": sl_p, "tp_price": tp_p, "risk_per_share": risk_per_share
            })

    except Exception as e: pass

swing_results.sort(key=lambda x: x['rs'], reverse=True)
short_term_results.sort(key=lambda x: x['rs'], reverse=True)

with open(HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(trade_history[-150:], f, indent=4)

# =============================================================================
# MODULE 6 — 總結算與 Discord 報告
# =============================================================================
print("⏳ [6/7] 正在結算戰績並發送 Discord 報告...")

def calculate_stats(history):
    closed = [t for t in history if '✅' in t['status'] or '❌' in t['status']]
    if not closed: return 0, 0, 0
    wins = [t for t in closed if '✅' in t['status']]
    return len(closed), len(wins), round(len(wins)/len(closed)*100, 1)

total_closed, wins, win_rate = calculate_stats(trade_history)

if DISCORD_SUMMARY_WEBHOOK:
    # 1. 今日結案明細
    detail_lines = []
    if closed_this_run:
        for t in closed_this_run:
            icon = "🎯" if "TAKE PROFIT" in t['status'] else "🛑"
            shares = 10000 / t['px']
            pnl = shares * (t['last_px'] - t['px'])
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            detail_lines.append(f"{icon} **{t['tk']}** ({t.get('tag', 'N/A')}): {pnl_str}")
    details_text = "\n".join(detail_lines) if detail_lines else "今日無新結案交易。"

    # 2. 目前持倉浮盈
    open_trades = [t for t in trade_history if t.get('status') == 'OPEN']
    floating_pnl = sum([(10000 / t['px']) * (t['last_px'] - t['px']) for t in open_trades])
    floating_str = f"+${floating_pnl:.2f}" if floating_pnl >= 0 else f"-${abs(floating_pnl):.2f}"
    floating_color = 65280 if floating_pnl >= 0 else 16711680

    # 3. 細分策略 P&L 結算 (歷史總計)
    strategy_stats = {}
    for t in [x for x in trade_history if '✅' in x['status'] or '❌' in x['status']]:
        tag = t.get('tag', '未分類')
        if tag not in strategy_stats: strategy_stats[tag] = {'wins': 0, 'total': 0, 'pnl': 0}
        strategy_stats[tag]['total'] += 1
        if '✅' in t['status']: strategy_stats[tag]['wins'] += 1
        strategy_stats[tag]['pnl'] += (10000 / t['px']) * (t['last_px'] - t['px'])
    
    breakdown_lines = []
    for tag, st in strategy_stats.items():
        w_rate = round((st['wins'] / st['total']) * 100, 1) if st['total'] > 0 else 0
        pnl_s = f"+${st['pnl']:.0f}" if st['pnl'] >= 0 else f"-${abs(st['pnl']):.0f}"
        breakdown_lines.append(f"**{tag}**: {w_rate}% 勝率 | P&L: {pnl_s} ({st['total']}單)")
    breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "尚無足夠結案數據。"

    # 👇 【重點升級】：計算美日股掃描數量，並加入 Discord 欄位
    us_scan_count = len(us_tickers)
    jp_scan_count = len(jp_tickers)

    us_macro_str = f"狀態: **{us_status}**\n市寬: {us_breadth}%\n派發: {us_dist} 日\n掃描: **{us_scan_count} 隻**"
    jp_macro_str = f"狀態: **{jp_status}**\n市寬: {jp_breadth}%\n派發: {jp_dist} 日\n掃描: **{jp_scan_count} 隻**"

    payload = {
        "embeds": [{
            "title": f"📊 系統戰績與宏觀結算摘要 ({today_str})", 
            "description": f"**今日結案動態:**\n{details_text}\n\n**🔍 各策略歷史表現:**\n{breakdown_text}",
            "color": floating_color,
            "fields": [
                {"name": "🇺🇸 美股大盤 (SPX)", "value": us_macro_str, "inline": True},
                {"name": "🇯🇵 日股大盤 (N225)", "value": jp_macro_str, "inline": True},
                {"name": '\u200b', "value": '\u200b', "inline": False}, # 分隔行
                {"name": "📂 目前持倉", "value": f"{len(open_trades)} 隻", "inline": True},
                {"name": "🌊 總浮動盈虧", "value": f"**{floating_str}**", "inline": True},
                {"name": "📈 總勝率", "value": f"{win_rate}% ({wins}/{total_closed})", "inline": True}
            ],
            "footer": {"text": f"每單本金 $10,000 USD | 時光機回溯 {SIMULATE_DAYS_AGO} 日"}
        }]
    }
    try: requests.post(DISCORD_SUMMARY_WEBHOOK, json=payload)
    except: pass
# =============================================================================
# MODULE 7 — 生成 UAT 前端 HTML (雙分頁系統：Dashboard + Journal)
# =============================================================================
print("⏳ [7/7] 正在生成雙分頁量化儀表板...")

def get_unit(tk): return "¥" if tk.endswith(".T") else "$"

# 將 Python 字典轉為 JSON 字串，直接注入 JS，避免 fetch CORS 錯誤
js_payload_str = json.dumps(js_payload)
trade_history_str = json.dumps(trade_history)

html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <script src="https://cdn.tailwindcss.com"></script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <title>UAT QUANT ({today_str})</title>
</head>
<body class="bg-[#020617] text-slate-300 p-4 font-sans h-screen flex flex-col overflow-hidden">
    
    <header class="bg-slate-900 border border-slate-800 rounded-xl p-3 shrink-0 mb-3 shadow-lg flex flex-col gap-3 relative overflow-hidden">
        <div class="absolute -right-10 -top-10 opacity-5 pointer-events-none transform rotate-12">
            <span class="text-9xl font-black italic">UAT TEST</span>
        </div>
        
        <div class="flex justify-between items-center z-10">
            <div class="flex items-center gap-4">
                <div>
                    <h1 class="text-2xl font-black text-white italic tracking-tighter">UAT場 <span class="text-fuchsia-500">QUANT</span></h1>
                    <div class="mt-1 inline-block px-3 py-0.5 bg-fuchsia-500/20 border border-fuchsia-500/30 rounded-full text-fuchsia-400 text-[10px] font-black tracking-widest shadow-[0_0_15px_rgba(217,70,239,0.2)]">
                        🕰️ 時光機: {today_str}
                    </div>
                </div>
                <div class="flex gap-2 ml-6 bg-slate-950 p-1 rounded-lg border border-slate-800">
                    <button id="tabBtn-dashboard" onclick="switchTab('dashboard')" class="bg-indigo-600 text-white px-4 py-1.5 rounded-md font-bold text-sm shadow-md transition">📊 儀表板 (Dashboard)</button>
                    <button id="tabBtn-journal" onclick="switchTab('journal')" class="text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition">📜 交易日誌 (Journal)</button>
                </div>
            </div>
            <div class="text-xs font-black text-slate-500 bg-black/50 px-3 py-1 rounded-lg border border-slate-800">🌐 Dual-Market Macro Radar</div>
        </div>

        <div class="grid grid-cols-2 gap-4 z-10">
            <div class="flex items-center gap-2 bg-slate-800/30 p-2 rounded-lg border border-slate-800">
                <div class="w-12 text-center text-xs font-black text-slate-400 border-r border-slate-700">美股<br>SPX</div>
                <div class="flex-1 flex justify-between gap-2 px-2">
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">市寬</span><span class="text-xs font-bold {'text-emerald-400' if us_breadth>40 else 'text-red-400'}">{us_breadth}%</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">派發</span><span class="text-xs font-bold {'text-red-400' if us_dist>=5 else 'text-emerald-400'}">{us_dist}d</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">狀態</span><span class="text-[10px] font-bold px-1 rounded {us_color}">{us_status}</span></div>
                </div>
            </div>
            <div class="flex items-center gap-2 bg-slate-800/30 p-2 rounded-lg border border-slate-800">
                <div class="w-12 text-center text-xs font-black text-slate-400 border-r border-slate-700">日股<br>N225</div>
                <div class="flex-1 flex justify-between gap-2 px-2">
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">市寬</span><span class="text-xs font-bold {'text-emerald-400' if jp_breadth>40 else 'text-red-400'}">{jp_breadth}%</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">派發</span><span class="text-xs font-bold {'text-red-400' if jp_dist>=5 else 'text-emerald-400'}">{jp_dist}d</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">狀態</span><span class="text-[10px] font-bold px-1 rounded {jp_color}">{jp_status}</span></div>
                </div>
            </div>
        </div>
    </header>

    <main id="tab-dashboard" class="flex-1 flex gap-4 overflow-hidden z-10">
        <div class="w-1/3 flex flex-col gap-4 overflow-hidden">
            <div class="bg-slate-900 p-2 rounded-xl border border-slate-800 h-[200px] shrink-0 relative flex items-center justify-center shadow-lg">
                <div class="absolute top-2 left-3 z-10 flex gap-2 items-center">
                    <span class="text-xs font-bold text-slate-400">SPX Anatomy:</span>
                    <span class="text-[9px] bg-red-500/20 text-red-400 px-1 rounded border border-red-500/30">200MA</span>
                    <span class="text-[9px] text-emerald-400 ml-2">▲ FTD</span>
                </div>
                <img src="charts/SPY_Trend.png" class="max-h-full max-w-full object-contain">
            </div>

            <div class="bg-slate-900 rounded-xl border border-slate-800 flex-1 flex flex-col overflow-hidden shadow-lg">
                <div class="p-3 border-b border-slate-800 font-black text-fuchsia-400 flex justify-between items-center shrink-0">
                    <span>🎯 模擬推介信號 (點擊查看)</span>
                </div>
                <div class="overflow-y-auto flex-1 p-2 space-y-2" id="signal-list">
                    <div class="text-[10px] font-bold text-slate-500 uppercase ml-1 mt-2">🏆 波段策略 (Swing)</div>
                    {"".join([f'''
                    <div class="bg-slate-800/50 hover:bg-fuchsia-900/30 cursor-pointer border border-slate-700/50 hover:border-fuchsia-500/50 rounded-lg p-2 transition" onclick="loadContent('{d['tk']}')">
                        <div class="flex justify-between items-center">
                            <span class="font-black text-white text-sm">{d['tk']}</span>
                            <span class="text-[9px] bg-fuchsia-500/20 text-fuchsia-300 px-1.5 py-0.5 rounded">{d['tag']}</span>
                        </div>
                        <div class="flex justify-between text-[10px] text-slate-400 mt-1">
                            <span>RS: {d['rs']} (<span class="{ 'text-emerald-400' if d['mom']>0 else 'text-red-400'}">{'+' if d['mom']>0 else ''}{d['mom']}</span>)</span>
                            <span>現價: {get_unit(d['tk'])}{d['px']}</span>
                        </div>
                    </div>
                    ''' for d in swing_results]) if swing_results else '<p class="text-slate-600 italic text-xs px-2">無訊號</p>'}
                    
                    <div class="text-[10px] font-bold text-slate-500 uppercase ml-1 mt-4">⚡ 短線游擊 (Short Term)</div>
                    {"".join([f'''
                    <div class="bg-slate-800/50 hover:bg-amber-900/30 cursor-pointer border border-slate-700/50 hover:border-amber-500/50 rounded-lg p-2 transition" onclick="loadContent('{d['tk']}')">
                        <div class="flex justify-between items-center">
                            <span class="font-black text-white text-sm">{d['tk']}</span>
                            <span class="text-[9px] bg-amber-500/20 text-amber-300 px-1.5 py-0.5 rounded">{d['tag']}</span>
                        </div>
                        <div class="flex justify-between text-[10px] text-slate-400 mt-1">
                            <span>RS: {d['rs']}</span>
                            <span>現價: {get_unit(d['tk'])}{d['px']}</span>
                        </div>
                    </div>
                    ''' for d in short_term_results]) if short_term_results else '<p class="text-slate-600 italic text-xs px-2">無訊號</p>'}
                </div>
            </div>
        </div>

        <div class="w-2/3 flex flex-col gap-4 h-full">
            <div class="bg-slate-900 rounded-xl border border-slate-700 p-4 shrink-0 shadow-lg">
                <div class="flex justify-between items-center mb-3">
                    <div class="flex items-center gap-2">
                        <h3 class="text-sm font-black text-amber-500">🧮 專業部位計算機</h3>
                        <span id="calc_ticker_name" class="text-xs font-bold text-white bg-slate-700 px-2 py-0.5 rounded">-</span>
                        <a id="tv_out_link" href="#" target="_blank" class="hidden text-[10px] font-bold bg-blue-600/30 text-blue-400 border border-blue-500/50 hover:bg-blue-600 hover:text-white px-2 py-0.5 rounded transition">🔗 在 TV 開啟</a>
                    </div>
                    <div class="flex items-center gap-2">
                        <label class="text-[10px] text-slate-400 font-bold uppercase">總資金 (Account Size):</label>
                        <input type="number" id="acc_size" value="10000" class="bg-slate-800 border border-slate-600 text-white text-xs px-2 py-1 rounded w-24 text-right focus:outline-none focus:border-amber-500" onchange="updateCalculator()" onkeyup="updateCalculator()">
                    </div>
                </div>
                <div class="grid grid-cols-5 gap-3 text-center">
                    <div class="bg-slate-800/50 p-2 rounded-lg border border-slate-700">
                        <div class="text-[9px] text-slate-400 uppercase font-bold">進場現價</div>
                        <div class="font-black text-white text-lg" id="calc_entry">-</div>
                    </div>
                    <div class="bg-red-900/10 p-2 rounded-lg border border-red-900/50">
                        <div class="text-[9px] text-red-400 uppercase font-bold">嚴格止損 (-2.5 ATR)</div>
                        <div class="font-black text-red-400 text-lg" id="calc_sl">-</div>
                    </div>
                    <div class="bg-emerald-900/10 p-2 rounded-lg border border-emerald-900/50">
                        <div class="text-[9px] text-emerald-400 uppercase font-bold">目標止盈 (+4.5 ATR)</div>
                        <div class="font-black text-emerald-400 text-lg" id="calc_tp">-</div>
                    </div>
                    <div class="bg-amber-500/10 p-2 rounded-lg border border-amber-500/30 relative">
                        <div class="absolute -top-2 -right-2 bg-amber-500 text-black text-[8px] font-black px-1.5 py-0.5 rounded-full">1% Risk</div>
                        <div class="text-[9px] text-amber-500 uppercase font-bold">建議買入股數</div>
                        <div class="font-black text-amber-400 text-lg" id="calc_shares">-</div>
                    </div>
                    <div class="bg-slate-800/50 p-2 rounded-lg border border-slate-700">
                        <div class="text-[9px] text-slate-400 uppercase font-bold">總持倉成本 (佔比)</div>
                        <div class="font-black text-blue-300 text-lg" id="calc_cost">-</div>
                    </div>
                </div>
            </div>

            <div class="bg-slate-900 p-1 rounded-xl border border-slate-800 flex-1 relative shadow-lg" id="tv_chart_container">
                <div class="absolute inset-0 flex items-center justify-center text-slate-600 text-sm italic font-bold z-0 pointer-events-none">
                    請點擊左側信號以載入圖表
                </div>
            </div>
        </div>
    </main>

<main id="tab-journal" class="hidden flex-1 overflow-y-auto bg-slate-900 rounded-xl border border-slate-800 p-6 z-10 flex flex-col gap-6 shadow-lg">
        
        <div class="flex justify-between items-center border-b border-slate-800 pb-2">
            <h2 class="text-2xl font-black text-white flex items-center gap-2">📜 歷史交易結算與日誌</h2>
            <div class="text-xs text-slate-500">每單固定以 $10,000 基準結算盈虧</div>
        </div>

        <div class="grid grid-cols-4 gap-4" id="journal-stats"></div>

        <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4">
            <h3 class="font-black text-fuchsia-400 mb-3 flex items-center gap-2">🎯 按策略分析 (Strategy Performance)</h3>
            <div class="grid grid-cols-2 lg:grid-cols-4 gap-4" id="strategy-stats-container">
                </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4">
                <h3 class="font-black text-cyan-400 mb-3 flex items-center gap-2">📂 目前持倉 (Open Positions)</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-xs text-left whitespace-nowrap">
                        <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                            <tr>
                                <th class="p-2">日期</th><th class="p-2">代號</th><th class="p-2">策略</th>
                                <th class="p-2">進場指標</th><th class="p-2">現時指標</th>
                                <th class="p-2">買入價</th><th class="p-2">止損</th><th class="p-2">止盈</th><th class="p-2">現價</th>
                                <th class="p-2 text-right">浮動 P&L</th><th class="p-2 text-right">回報 (%)</th>
                            </tr>
                        </thead>
                        <tbody id="journal-open-tbody"></tbody>
                    </table>
                </div>
            </div>

            <div class="bg-slate-800/30 rounded-xl border border-slate-700 p-4">
                <h3 class="font-black text-emerald-400 mb-3 flex items-center gap-2">📁 最近結案紀錄 (Closed Trades)</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-xs text-left whitespace-nowrap">
                        <thead class="text-slate-500 uppercase border-b border-slate-700 bg-slate-800/50">
                            <tr>
                                <th class="p-2">買入日期</th><th class="p-2">平倉日期</th><th class="p-2">代號</th>
                                <th class="p-2">策略</th><th class="p-2">狀態</th>
                                <th class="p-2">買入價</th><th class="p-2">賣出價</th>
                                <th class="p-2 text-right">實現 P&L</th><th class="p-2 text-right">回報 (%)</th>
                            </tr>
                        </thead>
                        <tbody id="journal-closed-tbody"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </main>

    <script>
        const rawData = {js_payload_str};
        const tradeHistory = {trade_history_str};
        
        let currentSelectedTicker = null;
        let tvWidget = null;

        function switchTab(tabId) {{
            document.getElementById('tab-dashboard').classList.toggle('hidden', tabId !== 'dashboard');
            document.getElementById('tab-journal').classList.toggle('hidden', tabId !== 'journal');
            
            document.getElementById('tabBtn-dashboard').className = tabId === 'dashboard' 
                ? 'bg-indigo-600 text-white px-4 py-1.5 rounded-md font-bold text-sm shadow-md transition' 
                : 'text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition';
                
            document.getElementById('tabBtn-journal').className = tabId === 'journal' 
                ? 'bg-indigo-600 text-white px-4 py-1.5 rounded-md font-bold text-sm shadow-md transition' 
                : 'text-slate-400 hover:text-white hover:bg-slate-800 px-4 py-1.5 rounded-md font-bold text-sm transition';

            if (tabId === 'journal') renderJournal();
        }}

        function loadContent(ticker) {{
            currentSelectedTicker = ticker;
            const isJp = ticker.endsWith('.T');
            const tvSymbol = isJp ? 'TSE:' + ticker.replace('.T', '') : ticker;

            const tvLink = document.getElementById('tv_out_link');
            tvLink.href = `https://www.tradingview.com/chart/?symbol=${{tvSymbol}}`;
            tvLink.classList.remove('hidden');

            if (tvWidget) {{ tvWidget.remove(); }}
            tvWidget = new TradingView.widget({{
                "autosize": true, "symbol": tvSymbol, "interval": "D", "timezone": "Etc/UTC",
                "theme": "dark", "style": "1", "locale": "en", "container_id": "tv_chart_container"
            }});

            updateCalculator();
        }}

        function updateCalculator() {{
            if (!currentSelectedTicker) return;
            const data = rawData.find(d => d.ticker === currentSelectedTicker);
            if (!data) return;

            const isJp = data.ticker.endsWith('.T');
            const unit = isJp ? '¥' : '$';

            document.getElementById('calc_ticker_name').innerText = data.ticker + " (" + data.tag + ")";
            const accountSize = parseFloat(document.getElementById('acc_size').value) || 10000;
            const riskAmount = accountSize * {MAX_ACCOUNT_RISK_PCT};
            
            let shares = Math.floor(riskAmount / data.risk_per_share);
            if (shares <= 0) shares = 0;
            
            const totalCost = shares * data.curr_price;
            const actualPosPct = (accountSize > 0) ? (totalCost / accountSize * 100).toFixed(1) : 0;
            
            document.getElementById('calc_entry').innerText = unit + data.curr_price.toFixed(2);
            document.getElementById('calc_sl').innerText = unit + data.sl_price.toFixed(2);
            document.getElementById('calc_tp').innerText = unit + data.tp_price.toFixed(2);
            document.getElementById('calc_shares').innerText = shares;
            document.getElementById('calc_cost').innerText = unit + totalCost.toLocaleString(undefined, {{maximumFractionDigits: 0}}) + " (" + actualPosPct + "%)\";
        }}

        function renderJournal() {{
            const openTbody = document.getElementById('journal-open-tbody');
            const closedTbody = document.getElementById('journal-closed-tbody');
            const statsContainer = document.getElementById('journal-stats');

            const sortedHist = [...tradeHistory].reverse();
            const opens = sortedHist.filter(t => t.status === 'OPEN');
            const closeds = sortedHist.filter(t => t.status !== 'OPEN');

            let totalClosedPnl = 0, wins = 0, totalOpenPnl = 0;
            
            closeds.forEach(t => {{
                totalClosedPnl += (10000 / t.px) * (t.last_px - t.px);
                if (t.status.includes('✅')) wins++;
            }});
            opens.forEach(t => {{
                totalOpenPnl += (10000 / t.px) * (t.last_px - t.px);
            }});

            const winRate = closeds.length > 0 ? ((wins / closeds.length) * 100).toFixed(1) : 0;
            const closedPct = closeds.length > 0 ? ((totalClosedPnl / (closeds.length * 10000)) * 100).toFixed(2) : "0.00";
            const openPct = opens.length > 0 ? ((totalOpenPnl / (opens.length * 10000)) * 100).toFixed(2) : "0.00";

            const closedSign = totalClosedPnl >= 0 ? '+' : '';
            const openSign = totalOpenPnl >= 0 ? '+' : '';
            const closedColor = totalClosedPnl >= 0 ? 'text-emerald-400' : 'text-red-400';
            const openColor = totalOpenPnl >= 0 ? 'text-emerald-400' : 'text-red-400';

            statsContainer.innerHTML = `
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700 text-center">
                    <div class="text-[10px] text-slate-400 uppercase font-bold mb-1">已結案總利潤</div>
                    <div class="text-2xl font-black ${{closedColor}}">${{closedSign}}$${{totalClosedPnl.toFixed(0)}} <span class="text-sm">(${{closedSign}}${{closedPct}}%)</span></div>
                </div>
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700 text-center">
                    <div class="text-[10px] text-slate-400 uppercase font-bold mb-1">歷史勝率</div>
                    <div class="text-2xl font-black text-white">${{winRate}}%</div>
                    <div class="text-[9px] text-slate-500 mt-1">${{wins}} 贏 / ${{closeds.length - wins}} 輸</div>
                </div>
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700 text-center">
                    <div class="text-[10px] text-slate-400 uppercase font-bold mb-1">目前未平倉</div>
                    <div class="text-2xl font-black text-cyan-400">${{opens.length}} 隻</div>
                </div>
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700 text-center">
                    <div class="text-[10px] text-slate-400 uppercase font-bold mb-1">總浮動盈虧</div>
                    <div class="text-2xl font-black ${{openColor}}">${{openSign}}$${{totalOpenPnl.toFixed(0)}} <span class="text-sm">(${{openSign}}${{openPct}}%)</span></div>
                </div>
            `;

            // ==========================================
            // 按策略 (Tag) 統計戰果
            // ==========================================
            const strategyStats = {{}};
            
            // 掃描所有已平倉交易
            closeds.forEach(t => {{
                const strat = t.tag || '未分類';
                
                if (!strategyStats[strat]) {{
                    strategyStats[strat] = {{ trades: 0, wins: 0, pnl: 0, deployed: 0 }};
                }}
                
                strategyStats[strat].trades += 1;
                if (t.status.includes('✅')) strategyStats[strat].wins += 1;
                
                // 計算此單 P&L 同動用資金 (固定 10k 基準)
                const tradePnl = (10000 / t.px) * (t.last_px - t.px);
                strategyStats[strat].pnl += tradePnl;
                strategyStats[strat].deployed += 10000;
            }});

            // 生成策略卡片 HTML (注意 JS 嘅 Template Literal $ 後面都要雙大括號)
            const strategyHtml = Object.keys(strategyStats).map(strat => {{
                const stats = strategyStats[strat];
                const stratWinRate = ((stats.wins / stats.trades) * 100).toFixed(1);
                const pColor = stats.pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                const pSign = stats.pnl >= 0 ? '+' : '';
                
                return `
                <div class="bg-slate-900/50 p-3 rounded-lg border border-slate-700/50 hover:border-fuchsia-500/50 transition">
                    <div class="text-xs font-black text-white mb-2 uppercase px-1 bg-slate-800 inline-block rounded">${{strat}}</div>
                    
                    <div class="flex justify-between text-[10px] text-slate-400 mb-1">
                        <span>勝率 (${{stats.wins}}/${{stats.trades}})</span>
                        <span class="font-bold text-white">${{stratWinRate}}%</span>
                    </div>
                    
                    <div class="flex justify-between text-[10px] text-slate-400 mb-1">
                        <span>已動用資金</span>
                        <span class="font-bold">$${{stats.deployed.toLocaleString()}}</span>
                    </div>
                    
                    <div class="flex justify-between text-[10px] text-slate-400 mt-2 pt-2 border-t border-slate-700">
                        <span>實現利潤</span>
                        <span class="font-black ${{pColor}}">${{pSign}}$${{stats.pnl.toFixed(0)}}</span>
                    </div>
                </div>
                `;
            }}).join('');

            document.getElementById('strategy-stats-container').innerHTML = 
                strategyHtml || '<div class="text-xs text-slate-500 italic p-2">暫無策略數據</div>';
            // ==========================================

            // 👇 填寫 Open Positions (加入 止損/止盈 價位)
            openTbody.innerHTML = opens.length === 0 ? '<tr><td colspan="11" class="p-4 text-center text-slate-500">目前無持倉</td></tr>' : opens.map(t => {{
                const pnl = (10000 / t.px) * (t.last_px - t.px);
                const pnlPct = ((t.last_px - t.px) / t.px * 100).toFixed(2);
                const pColor = pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
                const isJp = t.tk.endsWith('.T');
                const unit = isJp ? '¥' : '$';
                
                let metricStatus = '';
                if(t.curr_metric && t.entry_metric) {{
                    const currVal = parseInt(t.curr_metric.replace(/[^0-9-]/g, ''));
                    const entryVal = parseInt(t.entry_metric.replace(/[^0-9-]/g, ''));
                    metricStatus = currVal >= entryVal ? 'text-emerald-400' : 'text-red-400';
                }} else {{
                    metricStatus = 'text-slate-300';
                }}

                return `
                <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                    <td class="p-2">${{t.date}}</td>
                    <td class="p-2 font-bold text-white">${{t.tk}}</td>
                    <td class="p-2"><span class="text-[9px] bg-slate-700 px-1 rounded">${{t.tag || 'N/A'}}</span></td>
                    <td class="p-2 text-[10px] font-mono text-slate-400">${{t.entry_metric || '-'}}</td>
                    <td class="p-2 text-[10px] font-mono font-bold ${{metricStatus}}">${{t.curr_metric || '-'}}</td>
                    <td class="p-2">${{unit}}${{t.px}}</td>
                    <td class="p-2 text-red-400 font-mono">${{t.sl ? unit + t.sl : '-'}}</td>
                    <td class="p-2 text-emerald-400 font-mono">${{t.tp ? unit + t.tp : '-'}}</td>
                    <td class="p-2 text-white font-bold">${{unit}}${{t.last_px}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnl.toFixed(2)}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnlPct}}%</td>
                </tr>`;
            }}).join('');

            // 👇 填寫 Closed Trades (加入 買入日期、買入/賣出 價位)
            closedTbody.innerHTML = closeds.length === 0 ? '<tr><td colspan="9" class="p-4 text-center text-slate-500">無結案紀錄</td></tr>' : closeds.slice(0,50).map(t => {{
                const pnl = (10000 / t.px) * (t.last_px - t.px);
                const pnlPct = ((t.last_px - t.px) / t.px * 100).toFixed(2);
                const isWin = t.status.includes('✅');
                const pColor = isWin ? 'text-emerald-400' : 'text-red-400';
                const isJp = t.tk.endsWith('.T');
                const unit = isJp ? '¥' : '$';

                return `
                <tr class="border-b border-slate-700/50 hover:bg-slate-800 transition">
                    <td class="p-2 text-slate-400">${{t.date}}</td>
                    <td class="p-2">${{t.close_date || t.date}}</td>
                    <td class="p-2 font-bold text-white">${{t.tk}}</td>
                    <td class="p-2 text-[10px] text-slate-400">${{t.tag || 'N/A'}}</td>
                    <td class="p-2">${{isWin ? '🎯 止盈' : '🛑 止損'}}</td>
                    <td class="p-2">${{unit}}${{t.px}}</td>
                    <td class="p-2 text-white font-bold">${{unit}}${{t.last_px}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnl.toFixed(2)}}</td>
                    <td class="p-2 text-right font-black font-mono ${{pColor}}">${{pnl >= 0 ? '+' : ''}}${{pnlPct}}%</td>
                </tr>`;
            }}).join('');
        }}
    </script>
</body>
</html>"""

with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f: f.write(html)
print(f"\n🎉 UAT 時光機版建置完成！")
