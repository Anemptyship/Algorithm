import sys, time
from playwright.sync_api import sync_playwright
fails = []
def check(n, c, e=''):
    print(('PASS ' if c else 'FAIL ') + n + ((' :: ' + str(e)) if not c and e != '' else ''))
    if not c: fails.append(n)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path='/opt/pw-browsers/chromium-1194/chrome-linux/chrome', headless=True, args=['--no-sandbox'])
    pg = b.new_page(); errs = []; csp = []
    pg.on('pageerror', lambda e: errs.append(str(e))); pg.on('console', lambda m: csp.append(m.text) if 'Content Security' in m.text else None)
    pg.goto('http://127.0.0.1:8961/index.html'); time.sleep(2.6)
    items = pg.evaluate("[...document.querySelectorAll('.nav-more-menu a')].map(a=>a.getAttribute('href'))")
    check('menu has both entries, live first', items == ['/scail-live', '/scail-repair', '/ops-assistant'], items)
    check('menu items are not duplicated on later ticks', (time.sleep(1.6) or True) and pg.locator('.nav-more-menu a').count() == 3)
    links = pg.evaluate("[...document.querySelectorAll('[data-scail-live-card]')].map(a=>a.getAttribute('href'))")
    check('link only on the running SCAIL card', links == ['/scail-live?id=scail_' + 'a' * 32], links)
    check('no link on queued / wan / repair cards', pg.locator('[data-scail-live-card]').count() == 1)
    pg.evaluate("document.querySelector('[data-ui-job-id^=scail_aaaa]').setAttribute('data-state','done')"); time.sleep(1.5)
    check('link disappears when the job is no longer running', pg.locator('[data-scail-live-card]').count() == 0)
    pg.evaluate("document.querySelector('[data-ui-job-id^=scail_bbbb]').setAttribute('data-state','running')"); time.sleep(1.5)
    check('link appears when a job starts running', pg.locator('[data-scail-live-card]').count() == 1)
    check('no CSP violations / page errors', not csp and not errs, (csp, errs))
    b.close()
print('\nFAILED: ' + ', '.join(fails) if fails else '\nALL ENTRY CHECKS PASSED'); sys.exit(1 if fails else 0)
