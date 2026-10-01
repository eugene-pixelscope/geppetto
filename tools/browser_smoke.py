"""Run against a local demo server; never against operational services."""
from browser_guard import BASE, require_demo
require_demo()
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(BASE+'')
    page.wait_for_selector('.whale-card')
    page.evaluate('document.fonts.ready')
    page.screenshot(path=str(ROOT/'local/desktop.png'),full_page=True)
    assert 'Ball detection' not in page.locator('#nav').inner_text()
    assert '등번호' not in page.locator('#nav').inner_text()
    page.get_by_role('link',name='▤　Runs',exact=True).click()
    page.wait_for_selector('#category-filter')
    assert page.locator('#category-filter').input_value()==''
    assert page.locator('#filtered-runs .category-tag').filter(has_text='Ball detection').count()>0
    assert page.locator('#filtered-runs .category-tag').filter(has_text='Jersey recognition').count()>0
    page.locator('#category-filter').select_option('jersey-detection')
    page.get_by_text('No training history yet.',exact=True).wait_for()
    page.locator('#category-filter').select_option('ball-detection')

    page.wait_for_selector('.compare-check')
    complete=page.locator('tr').filter(has=page.locator('.badge.succeeded'))
    complete.nth(0).locator('.compare-check').check()
    complete.nth(1).locator('.compare-check').check()
    page.get_by_role('link',name='Compare selected',exact=True).click()
    page.wait_for_selector('#compare-chart polyline')
    page.screenshot(path=str(ROOT/'local/compare.png'),full_page=True)
    page.goto(BASE+'/#page=create&category=ball-detection')
    page.locator('#run-name').fill('브라우저 검증 실행')
    page.locator('#wizard-next').click()
    page.wait_for_selector('#wizard-model')
    page.locator('#wizard-next').click()
    page.wait_for_selector('#gpu-mode')
    page.locator('#wizard-next').click()
    page.get_by_role('heading',name='브라우저 검증 실행').wait_for()
    page.locator('#wizard-next').click()
    page.wait_for_selector('#run-chart')
    page.get_by_role('button',name='Stop training run').click()
    page.locator('#modal input[name=name]').fill('브라우저 검증 실행')
    page.get_by_role('button',name='Confirm',exact=True).click()
    page.wait_for_timeout(3500)
    page.locator('#refresh').click()
    page.get_by_text('Stopped',exact=True).wait_for()
    routes=['','page=jobs','page=datasets',
            'page=models','page=schedules','page=settings',
            'dataset=demo-ball-detection&category=ball-detection',
            'page=compare&category=ball-detection','category=ball-detection&tab=runs']
    for width in [320,375,768,1440]:
        page.set_viewport_size({'width':width,'height':900})
        for route in routes:
            page.goto(BASE+'/#'+route)
            page.wait_for_timeout(300)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(width,route)
    page.set_viewport_size({'width':375,'height':900})
    page.goto(BASE+'')
    page.wait_for_selector('.whale-card')
    page.screenshot(path=str(ROOT/'local/mobile.png'),full_page=True)
    assert not errors, errors
    print('PASS: create → final confirmation → stop; comparison; 9 routes × 4 widths; no JS errors')
    browser.close()
