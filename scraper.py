import os, json, sys, time, random, base64
from datetime import datetime, timezone, timedelta
from supabase import create_client
from playwright.sync_api import sync_playwright

try:
    cfg = json.loads(os.environ.get("APP_SECRET", "{}"))
    db = create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])
except Exception as e:
    print(f"INIT ERR: {e}")
    sys.exit(1)

W_ID = int(os.environ.get("WORKER_ID", "0"))
WR = cfg.get("WORKER_ROLES", {"CURRENT_MONTH": 5, "FORWARD_OLD": 10, "HISTORY_UPDATE": 5})
CR, FW = WR.get("CURRENT_MONTH", 5), WR.get("FORWARD_OLD", 10)
R = "C" if W_ID < CR else ("F" if W_ID < CR + FW else "H")
M_TIME = 3 * 60

def g_t(): return datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")

def g_max(my):
    try:
        r = db.table("cases").select("case_num").eq("month_year", my).neq("status", cfg.get("TXT_PRIV", "PRIV")).neq("status", cfg.get("TXT_PENDING", "PEND")).order("case_num", desc=True).limit(1).execute()
        return r.data[0]['case_num'] if r.data else 0
    except: return 0

def g_fwd(isc):
    mls, trgs, gl = [cfg["CURRENT_MONTH_STR"]] if isc else cfg.get("MONTHS_TO_SCAN", []), [], cfg.get("GAP_LIMIT", 400)
    for my in mls:
        mr, ex, st_idx = g_max(my), set(), 0
        while True:
            r = db.table("cases").select("case_num").eq("month_year", my).range(st_idx, st_idx+999).execute()
            if not r.data: break
            ex.update(x['case_num'] for x in r.data)
            st_idx += 1000
        gs, offset = (CR, W_ID) if isc else (FW, W_ID - CR)
        if gs == 0: continue
        trgs.extend({"c_id": f"{i}-{my[:2]}-{my[2:]}", "c_num": i, "m_y": my} for i in range(1, mr + gl + 1) if i not in ex and (i + int(my)) % gs == offset)
    random.shuffle(trgs)
    return trgs

def g_hst():
    trgs, st_idx, gs, offset = [], 0, WR.get("HISTORY_UPDATE", 5), W_ID - CR - FW
    if gs == 0: return trgs
    while True:
        r = db.table("cases").select("case_id,case_num,month_year,status,data_json").in_("status", [cfg.get("TXT_OPEN", "OPEN"), cfg.get("TXT_PENDING", "PEND")]).range(st_idx, st_idx+999).execute()
        if not r.data: break
        trgs.extend({"c_id": d['case_id'], "c_num": d['case_num'], "m_y": d['month_year'], "db_d": d} for d in r.data if int(d['case_id'].replace("-", "")) % gs == offset)
        st_idx += 1000
    random.shuffle(trgs)
    return trgs

def p_res(itm, suc, blk, dt):
    cid, nw, to, tp, tpd, te, isc = itm['c_id'], g_t(), cfg.get("TXT_OPEN", "OPEN"), cfg.get("TXT_PRIV", "PRIV"), cfg.get("TXT_PENDING", "PEND"), cfg.get("TXT_ERR", "E"), itm['m_y'] == cfg.get("CURRENT_MONTH_STR")
    sk = int(f"{itm['m_y'][2:]}{itm['m_y'][:2]}{itm['c_num']:05d}")
    
    if R in ["C", "F"]:
        st = dt.get(cfg.get("F_5", "5"), to) if suc else (tp if (not isc or (g_max(itm['m_y']) - itm['c_num'] >= cfg.get("GAP_LIMIT", 400))) else tpd) if blk else te
        if st != te: db.table("cases").upsert({"case_id": cid, "case_num": itm['c_num'], "month_year": itm['m_y'], "status": st, "data_json": dt if suc else {}, "last_checked": nw, "sort_key": sk}).execute()
    else:
        orw, ost = itm['db_d'], itm['db_d'].get("status")
        if not suc:
            if ost == tpd and blk and (g_max(itm['m_y']) - itm['c_num'] >= cfg.get("GAP_LIMIT", 400)): db.table("cases").update({"status": tp, "last_checked": nw, "sort_key": sk}).eq("case_id", cid).execute()
            return
        chg, hr = orw.get("data_json", {}) != dt, db.table("case_history").select("version_num").eq("case_id", cid).order("version_num", desc=True).limit(1).execute()
        nv, nst = (hr.data[0]['version_num'] + 1) if hr.data else 2, dt.get(cfg.get("F_5", "5"), to)
        db.table("cases").update({"data_json": dt, "status": nst, "last_checked": nw, "sort_key": sk}).eq("case_id", cid).execute()
        if ost != tpd: db.table("case_history").insert({"case_id": cid, "check_time": nw, "version_num": nv if chg else nv - 1, "is_changed": chg, "data_json": dt if chg else {}}).execute()

def r_main():
    rs, cerr = {"total": 0, "success": 0, "error": 0, "details": {}}, 0
    try: rps = g_fwd(True) if R == "C" else (g_fwd(False) if R == "F" else g_hst())
    except Exception as e: return print(f"W_{W_ID} DB Err: {e}")
    if not rps: return print(f"W_{W_ID}: No targets")

    st = time.time()
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            cx = br.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
            
            pg = cx.new_page()
            
            # Stealth Injection
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(pg)
            except Exception as e:
                print(f"W_{W_ID}: Stealth failed to load: {e}", flush=True)

            pg.set_default_navigation_timeout(60000)
            
            try:
                pg.goto(cfg["TARGET_URL"])
                try: pg.get_by_role("button", name=cfg.get("BTN_TXT", "btn")).click(timeout=8000)
                except: pass
                
                err_txt = cfg.get("TXT_ERR_MSG", "שגיאה במספר תיק")
                for itm in rps:
                    if time.time() - st > M_TIME or cerr >= 5: break
                    rs["total"] += 1
                    print(f"W_{W_ID} check {itm['c_num']} (M:{itm['m_y']})", flush=True)
                    suc, blk, sd = False, False, {}
                    
                    try:
                        pg.locator(cfg["INPUT_A"]).fill(str(itm['c_num']))
                        pg.locator(cfg["INPUT_B"]).fill(itm['m_y'])
                        pg.click(cfg["BTN_SUBMIT"])
                        
                        try: pg.wait_for_selector(cfg["STORE_ID"], state="attached", timeout=15000)
                        except Exception as we:
                            try: blk = err_txt in pg.content()
                            except: pass
                            raise we
                        
                        try:
                            sd[cfg.get("F_1","1")] = pg.locator(cfg.get("SEL_1","")).inner_text().strip()
                            sd[cfg.get("F_2","2")] = pg.locator(cfg.get("SEL_2","")).get_attribute("title") or pg.locator(cfg.get("SEL_2","")).inner_text().strip()
                            sd[cfg.get("F_3","3")] = pg.locator(cfg.get("SEL_3","")).inner_text().strip()
                            sd[cfg.get("F_4","4")] = pg.locator(cfg.get("SEL_4","")).inner_text().strip()
                        except: pass

                        jds = pg.locator(cfg["STORE_ID"]).get_attribute("value")
                        if jds and jds != "[]":
                            ci = json.loads(jds)[0]
                            sd[cfg.get("F_5","5")] = ci.get(cfg.get("API_K1","k1"), cfg.get("TXT_NO_STAT", "N/A"))
                            sd[cfg.get("F_6","6")] = ci.get(cfg.get("API_K2","k2"), "")
                            sd[cfg.get("F_7","7")] = []
                            pg.evaluate(cfg.get("POSTBACK_ACTION", "doPostBack()"))
                            pg.wait_for_selector(cfg.get("SEL_5",""), state="attached", timeout=10000)
                            pjs = pg.locator(cfg.get("SEL_5","")).get_attribute("value")
                            if pjs: sd[cfg.get("F_7","7")] = [{cfg.get("F_8","8"): pt.get(cfg.get("API_K3","k3"), ""), cfg.get("F_9","9"): pt.get(cfg.get("API_K4","k4"), ""), cfg.get("F_10","10"): pt.get(cfg.get("API_K5","k5"), "")} for pt in json.loads(pjs)]
                            suc, cerr = True, 0
                    except Exception as eloop:
                        if blk: cerr = 0
                        else:
                            rs["error"], cerr, ename = rs["error"] + 1, cerr + 1, type(eloop).__name__
                            try:
                                shot = pg.screenshot(type="jpeg", quality=40)
                                b64 = "data:image/jpeg;base64," + base64.b64encode(shot).decode('utf-8')
                                db.table("run_logs").insert({"worker_id": W_ID, "role": R, "errors_detail": {"screenshot": b64}}).execute()
                                print(f"W_{W_ID}: SCREENSHOT SAVED TO DB!", flush=True)
                            except Exception as e_db:
                                print(f"W_{W_ID}: FAILED TO SAVE SCREENSHOT: {e_db}", flush=True)
                            print(f"W_{W_ID} Err: {ename}", flush=True)
                    try: p_res(itm, suc, blk, sd)
                    except: pass
                    try: pg.goto(cfg["TARGET_URL"])
                    except: pass
            except Exception: pass
            finally: br.close()
    except Exception: pass

if __name__ == "__main__": r_main()
