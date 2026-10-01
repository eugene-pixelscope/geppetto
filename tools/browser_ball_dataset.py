"""Read-only browser check of the locally imported ball dataset."""
import json
from browser_guard import BASE, require_demo
require_demo()
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
item=json.loads((ROOT/'local/ball-dataset-import.json').read_text())
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    route=BASE+'/#dataset='+item['id']+'&category=ball-detection'
    page.goto(route)
    page.wait_for_selector('#sample-split')
    assert item['root'] in page.locator('main').inner_text()
    for split,count in item['splits'].items():
        page.locator('#sample-split').select_option(split)
        page.wait_for_function("count=>document.querySelector('#sample-count').textContent.includes(count.toLocaleString())",arg=count)
        page.wait_for_function("()=>document.querySelector('.preview-image')?.naturalWidth>0")
        page.wait_for_function("()=>document.querySelector('.label-summary')?.textContent.includes('Ground truth boxes')")
        assert page.locator('.label-overlay rect').count()>0
    page.locator('#sample-split').select_option('train')
    page.wait_for_function("()=>document.querySelector('#sample-count').textContent.includes('208,457')")
    page.locator('#sample-next').click()
    page.wait_for_function("()=>document.querySelector('#sample-page').value==='2'")
    page.wait_for_function("()=>document.querySelector('.preview-image')?.naturalWidth>0")
    page.get_by_role('button',name='View source labels').first.click()
    page.wait_for_selector('#modal[open]')
    assert len(page.locator('#modal pre').inner_text().split())>=8
    page.locator('#close-label').click()
    for width in [320,375,768,1440]:
        page.set_viewport_size({'width':width,'height':1000})
        page.wait_for_timeout(150)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
    page.screenshot(path=str(ROOT/'local/ball-dataset.png'),full_page=True)
    page.goto(BASE+'/#page=create&category=ball-detection&dataset='+item['id'])
    page.wait_for_selector('.dataset-check')
    assert page.locator('.dataset-check:checked').input_value()==item['id']
    page.locator('#run-name').fill('원본 데이터 연결 확인')
    for selector in ['#wizard-model','#gpu-mode']:
        page.locator('#wizard-next').click()
        page.wait_for_selector(selector)
    page.locator('#wizard-next').click()
    page.get_by_role('heading',name='원본 데이터 연결 확인').wait_for()
    assert 'simulated run' in page.locator('main').inner_text()
    assert item['name'] in page.locator('main').inner_text()
    assert not errors,errors
    print('PASS: real train/validation/test images + YOLO labels, pagination, 4 widths, training preview; no submission')
    browser.close()
