import os
from dotenv import load_dotenv
load_dotenv()

from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import api as shopify_api

import sys
import subprocess
app = FastAPI(title='Shopify Checker API')

_bot_process = None

@app.on_event("startup")
async def startup_event():
    global _bot_process
    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
    if os.path.exists(bot_path):
        _bot_process = subprocess.Popen([sys.executable, bot_path])
        print(f"Started bot.py with PID {_bot_process.pid}")
    else:
        print("bot.py not found, skipping bot startup.")

@app.on_event("shutdown")
async def shutdown_event():
    global _bot_process
    if _bot_process:
        _bot_process.terminate()
        _bot_process.wait()
@app.get('/')
def home():
    return {'message': 'Shopify checker API', 'status': True}

@app.get('/health')
def health():
    return {'status': 'ok'}

from fastapi.responses import PlainTextResponse

@app.get('/hits')
def view_hits(key: str = Query(None)):
    if key != 'shopihits99':
        raise HTTPException(status_code=403, detail="Forbidden")
    
    hits_file = '/sock/hits.txt' if os.path.isdir('/sock') else 'hits.txt'
    
    if not os.path.exists(hits_file):
        return PlainTextResponse("No hits recorded yet.")
    
    with open(hits_file, 'r') as f:
        content = f.read()
    
    return PlainTextResponse(content)

@app.get('/clear')
def clear_hits(key: str = Query(None)):
    if key != 'shopihits99':
        raise HTTPException(status_code=403, detail="Forbidden")
    
    hits_file = '/sock/hits.txt' if os.path.isdir('/sock') else 'hits.txt'
    
    # Clear the variant cache in api.py to force refetching cheapest products
    shopify_api._VARIANT_CACHE.clear()
    
    try:
        with open(hits_file, 'w') as f:
            f.write("")
        return {"message": "Hits and product cache cleared successfully", "status": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear: {str(e)}")

@app.get('/shopify')
async def shopify_checker(
    background_tasks: BackgroundTasks,
    site: str = Query(..., description="Shopify site domain or URL"),
    cc: str = Query(..., description="Card string in format CC|MM|YYYY|CVV"),
    proxy: Optional[str] = Query(None, description="Proxy string in format host:port or user:pass@host:port"),
    variant: Optional[str] = Query(None, description="Optional variant ID to use instead of auto-finding")
):
    cc_string = cc.strip()

    try:
        cc_parts = shopify_api.parse_cc_string(cc_string)
        card_number = cc_parts['cc']
        mes = cc_parts['mes']
        ano = cc_parts['ano']
        cvv = cc_parts['cvv']
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    success, message, gateway, price, currency = await shopify_api.process_card_async(
        card_number, mes, ano, cvv, site, variant, proxy
    )

    clean_response = shopify_api.extract_clean_response(message)
    hit_status = shopify_api._classify_status(success, clean_response)
    if hit_status:
        background_tasks.add_task(shopify_api.save_hit_to_file, cc_string, hit_status, gateway, str(price))

    price_value = 0.0
    try:
        price_value = float(price)
    except (ValueError, TypeError):
        if isinstance(price, str) and price.replace('.', '', 1).isdigit():
            price_value = float(price)

    return JSONResponse({
        'Gateway': gateway,
        'Price': price_value,
        'Response': clean_response,
        'Status': success,
        'cc': cc_string
    })

@app.get('/shopify/cheapest')
async def shopify_cheapest(
    site: str = Query(..., description="Shopify site domain or URL"),
    proxy: Optional[str] = Query(None, description="Proxy string in format host:port or user:pass@host:port")
):
    variant_id, price, product_handle, status = await shopify_api.find_cheapest_product(site, proxy)
    if not variant_id:
        raise HTTPException(status_code=400, detail=status or 'Failed to find product')

    return {
        'site': site,
        'variant_id': variant_id,
        'price': price,
        'product_handle': product_handle,
        'status': status
    }


class ChargeRequest(BaseModel):
    site: str
    cc: Optional[str] = None
    card_number: Optional[str] = None
    month: Optional[str] = None
    year: Optional[str] = None
    cvv: Optional[str] = None
    proxy: Optional[str] = None
    variant: Optional[str] = None


@app.post('/shopify')
async def shopify_post(payload: ChargeRequest, background_tasks: BackgroundTasks):
    # Accept either full `cc` string or individual card fields.
    cc_string = None
    if payload.cc:
        cc_string = payload.cc.strip()
    else:
        if not (payload.card_number and payload.month and payload.year and payload.cvv):
            raise HTTPException(status_code=400, detail='Provide `cc` or all card fields (`card_number`, `month`, `year`, `cvv`)')
        cc_string = f"{payload.card_number}|{payload.month}|{payload.year}|{payload.cvv}"

    try:
        cc_parts = shopify_api.parse_cc_string(cc_string)
        card_number = cc_parts['cc']
        mes = cc_parts['mes']
        ano = cc_parts['ano']
        cvv = cc_parts['cvv']
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    success, message, gateway, price, currency = await shopify_api.process_card_async(
        card_number, mes, ano, cvv, payload.site, payload.variant, payload.proxy
    )

    clean_response = shopify_api.extract_clean_response(message)
    hit_status = shopify_api._classify_status(success, clean_response)
    if hit_status:
        background_tasks.add_task(shopify_api.save_hit_to_file, cc_string, hit_status, gateway, str(price))

    price_value = 0.0
    try:
        price_value = float(price)
    except (ValueError, TypeError):
        if isinstance(price, str) and price.replace('.', '', 1).isdigit():
            price_value = float(price)

    return JSONResponse({
        'Gateway': gateway,
        'Price': price_value,
        'Response': clean_response,
        'Status': success,
        'cc': cc_string
    })


import paypal_checker

class PaypalRequest(BaseModel):
    cc: Optional[str] = None
    card_number: Optional[str] = None
    month: Optional[str] = None
    year: Optional[str] = None
    cvv: Optional[str] = None
    amount: str

@app.get('/paypal')
def paypal_checker_route(
    background_tasks: BackgroundTasks,
    cc: str = Query(..., description="Card string in format CC|MM|YYYY|CVV"),
    amount: str = Query(..., description="Amount to check")
):
    cc_string = cc.strip()
    try:
        cc_parts = paypal_checker.parse_cc_string(cc_string)
        card_number = cc_parts['cc']
        month = cc_parts['mes']
        year = cc_parts['ano']
        cvv = cc_parts['cvv']
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
        
    result = paypal_checker.process_paypal_card(card_number, month, year, cvv, amount)
    
    is_approved = False
    if isinstance(result, dict) and 'data' in result and result['data']:
        if result['data'].get('approveGuestPaymentWithCreditCard'):
            is_approved = True
            
    if is_approved:
        background_tasks.add_task(shopify_api.save_hit_to_file, cc_string, "APPROVED", "Paypal", amount)
        
    return JSONResponse(result)

@app.post('/paypal')
def paypal_post_route(
    payload: PaypalRequest,
    background_tasks: BackgroundTasks
):
    cc_string = None
    if payload.cc:
        cc_string = payload.cc.strip()
    else:
        if not (payload.card_number and payload.month and payload.year and payload.cvv):
            raise HTTPException(status_code=400, detail='Provide `cc` or all card fields (`card_number`, `month`, `year`, `cvv`)')
        cc_string = f"{payload.card_number}|{payload.month}|{payload.year}|{payload.cvv}"
        
    try:
        cc_parts = paypal_checker.parse_cc_string(cc_string)
        card_number = cc_parts['cc']
        month = cc_parts['mes']
        year = cc_parts['ano']
        cvv = cc_parts['cvv']
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
        
    result = paypal_checker.process_paypal_card(card_number, month, year, cvv, payload.amount)
    
    is_approved = False
    if isinstance(result, dict) and 'data' in result and result['data']:
        if result['data'].get('approveGuestPaymentWithCreditCard'):
            is_approved = True
            
    if is_approved:
        background_tasks.add_task(shopify_api.save_hit_to_file, cc_string, "APPROVED", "Paypal", payload.amount)
        
    return JSONResponse(result)

import gaypal

class GaypalRequest(BaseModel):
    cc: str
    mm: str
    yy: str
    cvx: str
    proxy: Optional[str] = None

@app.get('/gaypal')
async def gaypal_get_route(
    background_tasks: BackgroundTasks,
    cc: str = Query(..., description="Card number"),
    mm: str = Query(..., description="Expiry month (2 digits)"),
    yy: str = Query(..., description="Expiry year (2 or 4 digits)"),
    cvx: str = Query(..., description="CVV/CVC"),
    proxy: Optional[str] = Query(None, description="Proxy string")
):
    import uuid
    req_id = str(uuid.uuid4())[:8]
    try:
        result = gaypal.run_checkout(cc=cc.replace(" ", ""), mm=mm, yy=yy, cvx=cvx, req_id=req_id, proxy=proxy)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    cc_string = f"{cc}|{mm}|{yy}|{cvx}"
    if result.get("success"):
        background_tasks.add_task(shopify_api.save_hit_to_file, cc_string, "APPROVED", "Gaypal/WooCommerce", "0")

    return JSONResponse(result)

@app.post('/gaypal')
async def gaypal_post_route(payload: GaypalRequest, background_tasks: BackgroundTasks):
    import uuid
    req_id = str(uuid.uuid4())[:8]
    try:
        result = gaypal.run_checkout(
            cc=payload.cc.replace(" ", ""),
            mm=payload.mm,
            yy=payload.yy,
            cvx=payload.cvx,
            req_id=req_id,
            proxy=payload.proxy,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    cc_string = f"{payload.cc}|{payload.mm}|{payload.yy}|{payload.cvx}"
    if result.get("success"):
        background_tasks.add_task(shopify_api.save_hit_to_file, cc_string, "APPROVED", "Gaypal/WooCommerce", "0")

    return JSONResponse(result)


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 5000))
    uvicorn.run('app:app', host='0.0.0.0', port=port)
