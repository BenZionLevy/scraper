import os, json, sys, time, random
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from playwright.sync_api import sync_playwright

try:
    cfg = json.loads(os.environ.get("APP_SECRET", "{}"))
    db: Client = create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_KEY"])
except Exception as e:
    sys.exit(1)

# תוקן שם המשתנה כדי שהשרתים יקבלו את המספר שלהם מגיטהאב
W_ID = int(os.environ.get("WORKER_ID", "0"))
WR = cfg.get("WORKER_ROLES", {"CURRENT_MONTH": 5, "FORWARD_OLD": 10, "HISTORY_UPDATE": 5})
CR = WR.get("CURRENT_MONTH", 5)
FW = WR.get("FORWARD_OLD", 10)
R = "C" if W_ID < CR else ("F" if W_ID < CR + FW else "H")
M_TIME = 30 * 60

def g_t():
    return datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")

def g_max(my):
    try:
        tp = cfg.get("TXT_PRIV", "PRIV")
        tpd = cfg.get("TXT_PENDING", "PEND")
        r = db.table("cases").select("case_num").eq("month_year", my).neq("status", tp).neq("status", tpd).order("case_num", desc=True).limit(1).execute()
        return r.data[0]['case_num'] if r.data else 0
    except: return 0

def g_fwd(isc):
    mls = [cfg["CURRENT_MONTH_STR"]] if isc else cfg.get("MONTHS_TO_SCAN", [])
    trgs = []
    gl = cfg.get("GAP_LIMIT", 400)
    
    for my in mls:
        mr = g_max(my)
        ex = []
        st_idx = 0
        while True:
            r = db.table("cases").select("case_num").eq("month_year", my).range(st_idx, st_idx+999).execute()
            if not r.data: break
            ex.extend([x['case_num'] for x in r.data])
            st_idx += 1000
        ex = set(ex)
        
        group_size = CR if isc else FW
        offset = W_ID if isc else (W_ID - CR)
        
        if group_size == 0: continue
        
        for i in range(1, mr + gl + 1):
            if i not in ex:
                if (i + int(my)) % group_size == offset:
                    trgs.append({"c_id": f"{i}-{my[:2]}-{my[2:]}", "c_num": i, "m_y": my})
    
    random.shuffle(trgs)
    return trgs

def g_hst():
    to = cfg.get("TXT_OPEN", "OPEN")
    tpd = cfg.get("TXT_PENDING", "PEND")
    trgs = []
    st_idx = 0
    group_size = WR.get("HISTORY_UPDATE", 5)
    offset = W_ID - CR - FW
    
    if group_size == 0: return trgs
    
    while True:
        r = db.table("cases").select("case_id, case_num, month_year, status, data_json").in_("status", [to, tpd]).range(st_idx, st_idx+999).execute()
        if not r.data: break
        for d in r.data:
            hid = int(d['case_id'].replace("-", ""))
            if hid % group_size == offset:
                trgs.append({"c_id": d['case_id'], "c_num": d['case_num'], "m_y": d['month_year'], "db_d": d})
        st_idx += 1000
        
    random.shuffle(trgs)
    return trgs

def p_res(itm, suc, blk, dt):
    cid = itm['c_id']
    nw = g_t()
    to = cfg.get("TXT_OPEN", "OPEN")
    tp = cfg.get("TXT_PRIV", "PRIV")
    tpd = cfg.get("TXT_PENDING", "PEND")
    te = cfg.get("TXT_ERR", "E")
    gl = cfg.get("GAP_LIMIT", 400)
    isc = itm['m_y'] == cfg.get("CURRENT_MONTH_STR")
    f5 = cfg.get("F_5", "5")
    
    sk = int(f"{itm['m_y'][2:]}{itm['m_y'][:2]}{itm['c_num']:05d}")
    
    if R in ["C", "F"]: 
        if suc:
            st = dt.get(f5, to)
        else:
            if blk:
                if isc:
                    mr = g_max(itm['m_y'])
                    st = tp if (mr - itm['c_num'] >= gl) else tpd
                else:
                    st = tp
            else:
                st = te
        
        if st == te: return 
        
        d = {"case_id": cid, "case_num": itm['c_num'], "month_year": itm['m_y'], "status": st, "data_json": dt if suc else {}, "last_checked": nw, "sort_key": sk}
        db.table("cases").upsert(d).execute()

    else: 
        orw = itm['db_d']
        ost = orw.get("status")
        
        if not suc:
            if ost == tpd and blk:
                mr = g_max(itm['m_y'])
                if mr - itm['c_num'] >= gl:
                    db.table("cases").update({"status": tp, "last_checked": nw, "sort_key": sk}).eq("case_id", cid).execute()
            return 
        
        ojn = orw.get("data_json", {})
        chg = ojn != dt
        hr = db.table("case_history").select("version_num").eq("case_id", cid).order("version_num", desc=True).limit(1).execute()
        nv = (hr.data[0]['version_num'] + 1) if hr.data else 2
        nst = dt.get(f5, to)
        
        if ost == tpd:
            db.table("cases").update({"data_json": dt, "status": nst, "last_checked": nw, "sort_key": sk}).eq("case_id", cid).execute()
        else:
            if chg:
                db.table("cases").update({"data_json": dt, "status": nst, "last_checked": nw, "sort_key": sk}).eq("case_id", cid).execute()
                db.table("case_history").insert({"case_id": cid, "check_time": nw, "version_num": nv, "is_changed": True, "data_json": dt}).execute()
            else:
                db.table("case_history").insert({"case_id": cid, "check_time": nw, "version_num": nv - 1, "is_changed": False, "data_json": {}}).execute()

def r_main():
    run_stats = {"total": 0, "success": 0, "error": 0, "details": {}}
    
    try:
        rps = g_fwd(True) if R == "C" else (g_fwd(False) if R == "F" else g_hst())
    except Exception as e:
        try: db.table("run_logs").insert({"worker_id": W_ID, "role": R, "total_checked": 0, "success_count": 0, "error_count": 1, "errors_detail": {"INIT_ERR": str(e)[:50]}}).execute()
        except: pass
        return
        
    if not rps: 
        try: db.table("run_logs").insert({"worker_id": W_ID, "role": R, "total_checked": 0, "success_count": 0, "error_count": 0, "errors_detail": {"msg": "No targets"}}).execute()
        except: pass
        return

    st = time.time()
    
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            cx = br.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
            cx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            pg = cx.new_page()
            pg.set_default_navigation_timeout(60000)
            
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
                    run_stats["total"] += 1
                    suc = False
                    blk = False
                    sd = {}
                    
                    try:
                        pg.locator(cfg["INPUT_A"]).fill(str(itm['c_num']))
                        pg.locator(cfg["INPUT_B"]).fill(itm['m_y'])
                        pg.click(cfg["BTN_SUBMIT"])
                        
                        pg.wait_for_selector(cfg["STORE_ID"], state="attached", timeout=15000)
                        
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
                            run_stats["success"] += 1
                            
                    except Exception as eloop:
                        run_stats["error"] += 1
                        err_name = type(eloop).__name__
                        run_stats["details"][err_name] = run_stats["details"].get(err_name, 0) + 1

                    try:
                        p_res(itm, suc, blk, sd)
                    except: pass
                    
                    try: pg.goto(cfg["TARGET_URL"])
                    except: pass
                    time.sleep(1)
            except Exception as em:
                run_stats["error"] += 1
                err_str = str(em).lower()
                if "timeout" in err_str: e_type = "TIMEOUT"
                elif "locator" in err_str or "selector" in err_str: e_type = "ELEMENT_MISSING"
                elif "browser" in err_str or "context" in err_str: e_type = "BROWSER_CRASH"
                else: e_type = type(em).__name__
                run_stats["details"]["MAIN_" + e_type] = 1
            finally: 
                br.close()
                
    except Exception as global_e:
        run_stats["error"] += 1
        run_stats["details"]["GLOBAL"] = str(global_e)[:50]
    
    try:
        db.table("run_logs").insert({
            "worker_id": W_ID,
            "role": R,
            "total_checked": run_stats["total"],
            "success_count": run_stats["success"],
            "error_count": run_stats["error"],
            "errors_detail": run_stats["details"]
        }).execute()
    except: pass

if __name__ == "__main__":
    r_main()
