import json
import os
import time
import sys
from datetime import datetime
import zoneinfo
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding='utf-8')

W_ID = int(os.environ.get("WORKER_ID", "0"))
M_TIME = 5 * 60 
TZ = zoneinfo.ZoneInfo("Asia/Jerusalem")

try:
    cfg = json.loads(os.environ.get("APP_SECRET", "{}"))
    db: Client = create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])
except Exception as e:
    print(f"INIT ERROR: {repr(e)}")
    sys.exit(1)

r_cfg = cfg.get("WORKER_ROLES", {"CURRENT_MONTH": 5, "FORWARD_OLD": 10, "HISTORY_UPDATE": 5})
W_C = r_cfg.get("CURRENT_MONTH", 5)
W_F = r_cfg.get("FORWARD_OLD", 10)
W_H = r_cfg.get("HISTORY_UPDATE", 5)

if W_ID < W_C:
    R = "C"
    L_ID = W_ID
    T_L = W_C
elif W_ID < W_C + W_F:
    R = "F"
    L_ID = W_ID - W_C
    T_L = W_F
else:
    R = "H"
    L_ID = W_ID - (W_C + W_F)
    T_L = W_H

def g_t():
    return datetime.now(TZ).isoformat()

def g_max(m_y):
    rs = db.table("cases").select("case_num").eq("month_year", m_y).in_("status", [cfg.get("TXT_OPEN"), cfg.get("TXT_CLOSED")]).order("case_num", desc=True).limit(1).execute()
    return rs.data[0]['case_num'] if rs.data else 0

def g_fwd(is_curr=False):
    trgs = []
    gl = cfg.get("GAP_LIMIT", 400)
    cm = cfg.get("CURRENT_MONTH_STR", "")
    ms = [cm] if is_curr else cfg.get("MONTHS_TO_SCAN", [])

    for my in ms:
        if not is_curr and my == cm:
            continue

        m_all = db.table("cases").select("case_num").eq("month_year", my).order("case_num", desc=True).limit(1).execute()
        mc = m_all.data[0]['case_num'] if m_all.data else 0

        if not is_curr:
            mr = g_max(my)
            if mc - mr > gl:
                continue 
            
        if mc >= 90000:
            continue 

        sn = mc + 1
        
        for i in range(20): 
            tn = sn + L_ID + (i * T_L)
            if tn <= 90000:
                trgs.append({"c_id": f"{tn}-{my[:2]}-{my[2:]}", "c_num": tn, "m_y": my})

        if trgs:
            break 

    return trgs

def g_hst():
    rs = db.table("cases").select("*").in_("status", [cfg.get("TXT_OPEN"), cfg.get("TXT_PENDING")]).execute()
    ocs = rs.data
    mcs = [c for i, c in enumerate(ocs) if i % T_L == L_ID]
    
    trgs = []
    for c in mcs:
        trgs.append({"c_id": c["case_id"], "c_num": c["case_num"], "m_y": c["month_year"], "db_d": c})
    return trgs

def p_res(itm, suc, blk, dt):
    cid = itm['c_id']
    nw = g_t()
    to = cfg.get("TXT_OPEN")
    tp = cfg.get("TXT_PRIV")
    tpd = cfg.get("TXT_PENDING", "P")
    te = cfg.get("TXT_ERR", "E")
    gl = cfg.get("GAP_LIMIT", 400)
    isc = itm['m_y'] == cfg.get("CURRENT_MONTH_STR")
    f5 = cfg.get("F_5", "v5")
    
    if R in ["C", "F"]: 
        if suc:
            st = dt.get(f5, to)
        else:
            if blk:
                st = te
            else:
                if isc:
                    mr = g_max(itm['m_y'])
                    st = tp if (mr - itm['c_num'] >= gl) else tpd
                else:
                    st = tp
        
        if st == te: return 
        
        d = {"case_id": cid, "case_num": itm['c_num'], "month_year": itm['m_y'], "status": st, "data_json": dt if suc else {}, "last_checked": nw}
        db.table("cases").upsert(d).execute()

    else: 
        orw = itm['db_d']
        ost = orw.get("status")
        
        if not suc:
            if ost == tpd and not blk:
                mr = g_max(itm['m_y'])
                if mr - itm['c_num'] >= gl:
                    db.table("cases").update({"status": tp, "last_checked": nw}).eq("case_id", cid).execute()
            return 
        
        ojn = orw.get("data_json", {})
        chg = ojn != dt
        hr = db.table("case_history").select("version_num").eq("case_id", cid).order("version_num", desc=True).limit(1).execute()
        nv = (hr.data[0]['version_num'] + 1) if hr.data else 2
        nst = dt.get(f5, to)
        
        if ost == tpd:
            db.table("cases").update({"data_json": dt, "status": nst, "last_checked": nw}).eq("case_id", cid).execute()
        else:
            if chg:
                db.table("cases").update({"data_json": dt, "status": nst, "last_checked": nw}).eq("case_id", cid).execute()
                db.table("case_history").insert({"case_id": cid, "check_time": nw, "version_num": nv, "is_changed": True, "data_json": dt}).execute()
            else:
                db.table("case_history").insert({"case_id": cid, "check_time": nw, "version_num": nv - 1, "is_changed": False, "data_json": {}}).execute()

def r_main():
    print(f"W_ID {W_ID} - {R} init...")
    try:
        rps = g_fwd(True) if R == "C" else (g_fwd(False) if R == "F" else g_hst())
    except Exception as e:
        print(f"Err g: {repr(e)}")
        return
        
    if not rps: 
        print("No trgs")
        return

    print(f"Found {len(rps)} trgs")
    st = time.time()
    with sync_playwright() as p:
        try:
            br = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            cx = br.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
            cx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            pg = cx.new_page()
            pg.set_default_navigation_timeout(60000)
        except Exception as e:
            print(f"Err br: {repr(e)}")
            return
        
        try:
            pg.goto(cfg["TARGET_URL"])
            try: pg.get_by_role("button", name=cfg.get("BTN_TXT", "btn")).click(timeout=8000)
            except: pass
            time.sleep(1)
            
            f1, f2, f3, f4, f6, f7, f8, f9, f10 = cfg.get("F_1","1"), cfg.get("F_2","2"), cfg.get("F_3","3"), cfg.get("F_4","4"), cfg.get("F_6","6"), cfg.get("F_7","7"), cfg.get("F_8","8"), cfg.get("F_9","9"), cfg.get("F_10","10")
            ak1, ak2, ak3, ak4, ak5 = cfg.get("API_K1","k1"), cfg.get("API_K2","k2"), cfg.get("API_K3","k3"), cfg.get("API_K4","k4"), cfg.get("API_K5","k5")
            s1, s2, s3, s4, s5 = cfg.get("SEL_1",""), cfg.get("SEL_2",""), cfg.get("SEL_3",""), cfg.get("SEL_4",""), cfg.get("SEL_5","")
            
            for itm in rps:
                if time.time() - st > M_TIME: break
                suc = False
                blk = False
                sd = {}
                
                try:
                    pg.locator(cfg["INPUT_A"]).fill(str(itm['c_num']))
                    pg.locator(cfg["INPUT_B"]).fill(itm['m_y'])
                    pg.click(cfg["BTN_SUBMIT"])
                    
                    try:
                        pg.wait_for_selector(cfg["STORE_ID"], state="attached", timeout=15000)
                    except Exception as we:
                        pt = pg.content().lower()
                        if any(x in pt for x in cfg.get("ERR_WORDS", [])): blk = True
                        raise we

                    try:
                        sd[f1] = pg.locator(s1).inner_text().strip()
                        ttl = pg.locator(s2).get_attribute("title")
                        sd[f2] = ttl if ttl else pg.locator(s2).inner_text().strip()
                        sd[f3] = pg.locator(s3).inner_text().strip()
                        sd[f4] = pg.locator(s4).inner_text().strip()
                    except: pass

                    jds = pg.locator(cfg["STORE_ID"]).get_attribute("value")
                    if jds and jds != "[]":
                        ci = json.loads(jds)[0]
                        sd[cfg.get("F_5", "5")] = ci.get(ak1, cfg.get("TXT_NO_STAT", "N/A"))
                        sd[f6] = ci.get(ak2, "")
                        sd[f7] = []
                        
                        pg.evaluate(cfg["POSTBACK_ACTION"])
                        pg.wait_for_selector(s5, state="attached", timeout=10000)
                        pjs = pg.locator(s5).get_attribute("value")
                        
                        if pjs:
                            for pt in json.loads(pjs):
                                sd[f7].append({f8: pt.get(ak3, ""), f9: pt.get(ak4, ""), f10: pt.get(ak5, "")})
                        suc = True
                except Exception: 
                    pass

                try:
                    p_res(itm, suc, blk, sd)
                except Exception as e_pres:
                    print(f"Err p_res: {repr(e_pres)}")
                
                try:
                    pg.goto(cfg["TARGET_URL"])
                except: pass
                time.sleep(1)
        except Exception as em:
            print(f"Err main loop: {repr(em)}")
        finally: 
            br.close()

if __name__ == "__main__":
    r_main()
