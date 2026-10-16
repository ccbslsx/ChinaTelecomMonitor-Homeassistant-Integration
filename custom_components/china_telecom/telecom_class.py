import re
import base64
import json
import requests
from datetime import datetime
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
import logging 
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import certifi

_LOGGER = logging.getLogger(__name__)

#: Traffic units expressed in KB, the unit this module works in internally.
TRAFFIC_UNITS_KB = {
    "B": 1.0 / 1024.0,
    "KB": 1.0,
    "MB": 1024.0,
    "GB": 1024.0 ** 2,
    "TB": 1024.0 ** 3,
}

#: Matches a traffic figure with its unit inside arbitrary API wording, e.g.
#: "1.07GB", "0KB", "/共10.00GB" or "达40GB降速". No \b is used because the
#: characters following the unit are often CJK, which Python counts as word
#: characters.
TRAFFIC_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)", re.IGNORECASE)
BARE_NUMBER_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")

#: flowList entries already carry a unit-free GB figure in rightTitleEnd
#: ("/61.07GB"); keep both as a defensive fallback for the "已用 + 降速" shape.
DOWNSHIFT_PATTERN = re.compile(r"(\d+(?:\.\d+)?\s*[KMGT]B)", re.IGNORECASE)


class TelecomSSLAdapter(HTTPAdapter):
    """自定义适配器解决SSL证书问题"""
    def __init__(self):
        # 首先定义 ssl_context 属性
        self.ssl_context = None
        
        # 然后调用父类初始化
        super().__init__()
        
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        # 延迟创建 SSL 上下文（第一次使用时创建）
        if self.ssl_context is None:
            # 创建 SSL 上下文
            self.ssl_context = create_urllib3_context()
            # 降低安全级别以兼容旧服务器
            self.ssl_context.set_ciphers('DEFAULT:@SECLEVEL=1')
            # 加载证书
            self.ssl_context.load_verify_locations(cafile=certifi.where())
        
        # 使用创建的 SSL 上下文
        pool_kwargs['ssl_context'] = self.ssl_context
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

class Telecom:
    def __init__(self):
        self.login_info = {}
        self.phonenum = None
        self.password = None
        self.token = None
        self.login_client_type = "#12.2.0#channel50#iPhone 14 Pro#"
        self.query_client_type = "#12.2.0#channel50#iPhone 14 Pro#"
        self.client_type = self.query_client_type
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }
        
        # 创建会话并使用自定义适配器
        self.session = requests.Session()
        adapter = TelecomSSLAdapter()
        self.session.mount("https://", adapter)
        
        # 设置验证使用 certifi
        self.session.verify = certifi.where()

    def _mask_value(self, value):
        if value is None:
            return None
        text = str(value)
        if self.phonenum:
            text = text.replace(self.phonenum, f"{self.phonenum[:3]}****{self.phonenum[7:]}")
            text = text.replace(self.trans_number(self.phonenum), "***encoded_phone***")
        if self.password:
            text = text.replace(self.password, "***password***")
            text = text.replace(self.trans_number(self.password), "***encoded_password***")
        text = re.sub(r"\b(1[3-9]\d)\d{4}(\d{4})\b", r"\1****\2", text)
        if len(text) <= 8:
            return "***"
        return f"{text[:4]}***{text[-4:]}"

    def sanitize_for_log(self, data):
        sensitive_keys = {
            "account",
            "androidid",
            "authentication",
            "deviceid",
            "deviceuid",
            "loginauthcipherasymmertric",
            "password",
            "phonenum",
            "phoneNum",
            "sharephonenum",
            "token",
            "userId",
            "userid",
            "userloginname",
        }
        if isinstance(data, dict):
            sanitized = {}
            for key, value in data.items():
                key_lower = str(key).lower()
                if key_lower in sensitive_keys or "phone" in key_lower or "token" in key_lower:
                    sanitized[key] = self._mask_value(value)
                else:
                    sanitized[key] = self.sanitize_for_log(value)
            return sanitized
        if isinstance(data, list):
            return [self.sanitize_for_log(item) for item in data]
        if isinstance(data, str):
            text = data
            if self.phonenum:
                text = text.replace(self.phonenum, f"{self.phonenum[:3]}****{self.phonenum[7:]}")
                text = text.replace(self.trans_number(self.phonenum), "***encoded_phone***")
            if self.password:
                text = text.replace(self.password, "***password***")
                text = text.replace(self.trans_number(self.password), "***encoded_password***")
            return re.sub(r"\b(1[3-9]\d)\d{4}(\d{4})\b", r"\1****\2", text)
        return data

    def format_for_log(self, data):
        try:
            return json.dumps(self.sanitize_for_log(data), ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(self.sanitize_for_log(str(data)))

    def _response_json(self, response, context):
        try:
            payload = response.json()
        except ValueError:
            payload = {
                "error": "non_json_response",
                "status_code": response.status_code,
                "text": response.text,
            }
            _LOGGER.error(
                "China Telecom %s returned non-JSON response: status=%s headers=%s body=%s",
                context,
                response.status_code,
                self.format_for_log(dict(response.headers)),
                self.format_for_log(response.text),
            )
            return payload

        _LOGGER.debug(
            "China Telecom %s response: status=%s body=%s",
            context,
            response.status_code,
            self.format_for_log(payload),
        )
        return payload

    def set_login_info(self, login_info):
        self.login_info = login_info
        self.phonenum = login_info.get("phonenum", None)
        self.password = login_info.get("password", None)
        self.token = login_info.get("token", None)

    def trans_number(self, phonenum, encode=True):
        result = ""
        caesar_size = 2 if encode else -2
        for char in phonenum:
            result += chr(ord(char) + caesar_size & 65535)
        return result

    def encrypt(self, str):
        public_key_pem = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBkLT15ThVgz6/NOl6s8GNPofd
WzWbCkWnkaAm7O2LjkM1H7dMvzkiqdxU02jamGRHLX/ZNMCXHnPcW/sDhiFCBN18
qFvy8g6VYb9QtroI09e176s+ZCtiv7hbin2cCTj99iUpnEloZm19lwHyo69u5UMi
PMpq0/XKBO8lYhN/gwIDAQAB
-----END PUBLIC KEY-----"""
        public_key = RSA.import_key(public_key_pem.encode())
        cipher = PKCS1_v1_5.new(public_key)
        ciphertext = cipher.encrypt(str.encode())
        encoded_ciphertext = base64.b64encode(ciphertext).decode()
        return encoded_ciphertext

    def get_fee_flow_limit(self, fee_remain_flow):
        today = datetime.today()
        days_in_month = (
            datetime(today.year, today.month + 1, 1)
            - datetime(today.year, today.month, 1)
        ).days
        return int((fee_remain_flow / days_in_month))

    def do_login(self, phonenum, password, telecom_device_id=""):
        phonenum = str(phonenum or self.phonenum)
        password = str(password or self.password)
        telecom_device_id = str(telecom_device_id or "").strip()
        self.phonenum = phonenum
        self.password = password
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        system_version = "15.4.0"
        # Keep server-side device identity stable. Do not generate a random id per login.
        device_uid = f"3{phonenum}"
        trusted_device_for_sign = telecom_device_id[:12] if telecom_device_id else phonenum
        enc_str = (
            f"iPhone 14 {system_version}"
            f"{trusted_device_for_sign}{phonenum}{ts}{password}0$$$0."
        )
        body = {
            "content": {
                "fieldData": {
                    "accountType": "",
                    "authentication": self.trans_number(password),
                    "deviceUid": device_uid,
                    "isChinatelecom": "0",
                    "loginAuthCipherAsymmertric": self.encrypt(enc_str),
                    "loginType": "4",
                    "phoneNum": self.trans_number(phonenum),
                    "systemVersion": system_version,
                    "androidId": self.trans_number(telecom_device_id) if telecom_device_id else "",
                },
                "attach": "iPhone",
            },
            "headerInfos": {
                "code": "userLoginNormal",
                "clientType": self.login_client_type,
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": self.trans_number(phonenum),
            },
        }
        try:
            response = self.session.post(
                "https://appgologin.189.cn:9031/login/client/userLoginNormal",
                headers=self.headers,
                json=body,
                timeout=30
            )
            response.raise_for_status()
            return self._response_json(response, "login")
        except requests.exceptions.SSLError as ssl_err:
            _LOGGER.error(f"SSL验证失败: {ssl_err}")
            _LOGGER.warning("尝试临时禁用SSL验证...")
            # 使用独立的会话临时禁用验证
            temp_session = requests.Session()
            response = temp_session.post(
                "https://appgologin.189.cn:9031/login/client/userLoginNormal",
                headers=self.headers,
                json=body,
                verify=False,
                timeout=30
            )
            response.raise_for_status()
            return self._response_json(response, "login without SSL verification")
        except requests.exceptions.RequestException as req_err:
            _LOGGER.error(f"请求失败: {req_err}")
            result = {"error": str(req_err)}
            response = getattr(req_err, "response", None)
            if response is not None:
                result["status_code"] = response.status_code
                result["text"] = response.text
            return result

    def qry_important_data(self, **kwargs):
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        account = self.phonenum
        body = {
            "content": {
                "fieldData": {
                    "provinceCode": self.login_info.get("provinceCode") or "600101",
                    "cityCode": self.login_info.get("cityCode") or "8441900",
                    "shopId": "20002",
                    "isChinatelecom": "0",
                    "account": self.trans_number(account),
                }
            },
            "headerInfos": {
                "code": "qryImportantData",
                "clientType": self.query_client_type,
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": self.trans_number(account),
                "token": kwargs.get("token") or self.token,
            },
        }
        try:
            response = self.session.post(
                "https://appfuwu.189.cn:9021/query/qryImportantData",
                headers=self.headers,
                json=body,
                timeout=30
            )
            response.raise_for_status()
            return self._response_json(response, "qryImportantData")
        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"查询重要数据失败: {e}")
            result = {"error": str(e)}
            response = getattr(e, "response", None)
            if response is not None:
                result["status_code"] = response.status_code
                result["text"] = response.text
            return result

    def user_flux_package(self, **kwargs):
        billing_cycle = kwargs.get("billing_cycle") or datetime.now().strftime("%Y%m")
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        body = {
            "content": {
                "fieldData": {
                    "queryFlag": "0",
                    "accessAuth": "1",
                    "account": self.trans_number(self.phonenum),
                },
                "attach": "test",
            },
            "headerInfos": {
                "code": "userFluxPackage",
                "clientType": self.client_type,
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": self.trans_number(self.phonenum),
                "token": kwargs.get("token") or self.token,
            },
        }
        try:
            response = self.session.post(
                "https://appfuwu.189.cn:9021/query/userFluxPackage",
                headers=self.headers,
                json=body,
                timeout=30
            )
            response.raise_for_status()
            return self._response_json(response, "userFluxPackage")
        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"查询流量包失败: {e}")
            return {"error": str(e)}

    def qry_share_usage(self, **kwargs):
        billing_cycle = kwargs.get("billing_cycle") or datetime.now().strftime("%Y%m")
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        body = {
            "content": {
                "attach": "test",
                "fieldData": {
                    "billingCycle": billing_cycle,
                    "account": self.trans_number(self.phonenum),
                },
            },
            "headerInfos": {
                "code": "qryShareUsage",
                "clientType": self.client_type,
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": self.trans_number(self.phonenum),
                "token": kwargs.get("token") or self.token,
            },
        }
        try:
            response = self.session.post(
                "https://appfuwu.189.cn:9021/query/qryShareUsage",
                headers=self.headers,
                json=body,
                timeout=30
            )
            response.raise_for_status()
            data = self._response_json(response, "qryShareUsage")
            # 返回的号码字段加密，需做解密转换
            if data.get("responseData") and data.get("responseData").get(
                "data", {}
            ).get("sharePhoneBeans", []):
                for item in data["responseData"]["data"]["sharePhoneBeans"]:
                    item["sharePhoneNum"] = self.trans_number(item["sharePhoneNum"], False)
                for share_type in data["responseData"]["data"]["shareTypeBeans"]:
                    for share_info in share_type["shareUsageInfos"]:
                        for share_amount in share_info["shareUsageAmounts"]:
                            share_amount["phoneNum"] = self.trans_number(
                                share_amount["phoneNum"], False
                            )
            return data
        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"查询共享使用情况失败: {e}")
            return {}

    @staticmethod
    def _to_number(value, default=0):
        if value is None or isinstance(value, bool):
            return default
        try:
            if isinstance(value, str):
                value = value.strip().replace(",", "")
                if not value:
                    return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def parse_traffic_kb(cls, value):
        """Parse an API traffic value into KB.

        Handles the many shapes the China Telecom API uses: bare numbers and
        numeric strings (already KB), plus decorated strings such as "1.07GB",
        "0KB", "/共10.00GB" or "达40GB降速". Returns 0 when nothing can be
        recognised, so a malformed field can never poison a total.
        """
        if value is None or isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value)

        text = str(value).strip()
        if not text:
            return 0

        match = TRAFFIC_PATTERN.search(text)
        if match:
            size, unit = float(match.group(1)), match.group(2).upper()
            return int(round(size * TRAFFIC_UNITS_KB[unit]))

        # No unit: the API reports these already in KB ("27259260").
        match = BARE_NUMBER_PATTERN.search(text.replace(",", ""))
        if match and not re.search(r"[A-Za-z%]", text):
            return int(float(match.group(1)))
        return 0

    def parse_flow_list(self, flow_list):
        """Normalise flowInfo.flowList into [{name, use, balance, total}] in KB.

        This is the per-line breakdown the app shows on its home page and it is
        the only place a total is available for accounts whose
        totalAmount.total/balance come back as "0" (e.g. 达量降速 plans).
        """
        items = []
        for item in flow_list or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if "流量" not in title:
                continue

            left_title = str(item.get("leftTitle") or "")
            right_title = str(item.get("rightTitle") or "")
            right_end = str(item.get("rightTitleEnd") or "")

            try:
                # 常规流量
                if "已用" in left_title and "剩余" in right_title:
                    item_use = self.parse_traffic_kb(item.get("leftTitleHh"))
                    item_balance = self.parse_traffic_kb(item.get("rightTitleHh"))
                    item_total = item_use + item_balance
                # 常规流量，超流量
                elif "超出" in left_title and "/" in right_end:
                    item_balance = -self.parse_traffic_kb(item.get("leftTitleHh"))
                    item_total = self.parse_traffic_kb(right_end.split("/")[-1])
                    item_use = item_total - item_balance
                # 无限流量，达量降速
                elif "已用" in left_title and "降速" in right_title:
                    match = DOWNSHIFT_PATTERN.search(right_title)
                    item_total = self.parse_traffic_kb(match.group(1)) if match else 0
                    item_use = self.parse_traffic_kb(item.get("leftTitleHh"))
                    item_balance = max(item_total - item_use, 0)
                else:
                    _LOGGER.debug("Ignore flow entry: %s", item)
                    continue
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Failed to parse flow entry: %s", item, exc_info=True)
                continue

            items.append(
                {
                    "name": title,
                    "use": item_use,
                    "balance": item_balance,
                    "total": item_total,
                }
            )
        return items

    def parse_flux_packages(self, flux_data):
        """Normalise userFluxPackage into per-package and per-group totals.

        Returns (packages, groups, plan_name, share_tip) with all traffic values
        in KB. ``groups`` is keyed by the package group title the API itself
        uses ("国内通用流量" and friends) and each entry carries a ``kind`` of
        "通用" or "专用" so callers can total the general-purpose pool.
        """
        packages = []
        groups = {}
        plan_name = None
        share_tip = None

        if not isinstance(flux_data, dict):
            return packages, groups, plan_name, share_tip

        data = (flux_data.get("responseData") or {}).get("data")
        if not isinstance(data, dict):
            return packages, groups, plan_name, share_tip

        main_product = data.get("mainProductOFFInfo")
        if isinstance(main_product, dict):
            plan_name = str(main_product.get("productOFFName") or "").strip() or None
            share_tip = str(main_product.get("shareTipDesc") or "").strip() or None

        ratable = data.get("productOFFRatable")
        if not isinstance(ratable, dict):
            return packages, groups, plan_name, share_tip

        for group in ratable.get("ratableResourcePackages") or []:
            if not isinstance(group, dict):
                continue
            title = str(group.get("title") or "未分类").strip()
            # The API groups every general-purpose pack (base plan, rollover and
            # add-on packs) under a title containing 通用; anything else is
            # treated as dedicated/directional traffic.
            kind = "通用" if "通用" in title else "专用"
            bucket = groups.setdefault(
                title,
                {"title": title, "kind": kind, "use": 0, "balance": 0, "total": 0, "count": 0},
            )

            for item in group.get("productInfos") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("isInvalid") or "0") == "1":
                    continue

                used = self.parse_traffic_kb(item.get("leftHighlight"))
                balance = self.parse_traffic_kb(item.get("rightHighlight"))
                total = self.parse_traffic_kb(item.get("rightCommon"))
                if total <= 0:
                    total = used + balance

                packages.append(
                    {
                        "name": str(item.get("title") or "").strip(),
                        "group": title,
                        "kind": kind,
                        "use": used,
                        "balance": balance,
                        "total": total,
                        "expire": item.get("outOfServiceTime") or None,
                        "unlimited": str(item.get("isInfiniteAmount") or "0") == "1",
                    }
                )
                bucket["use"] += used
                bucket["balance"] += balance
                bucket["total"] += total
                bucket["count"] += 1

        return packages, groups, plan_name, share_tip

    def to_summary(self, data, phonenum="", flux_data=None):
        if not data:
            return {}
        phonenum = phonenum or self.phonenum

        flow_info = data.get("flowInfo") or {}
        voice_info = data.get("voiceInfo") or {}
        integral_info = data.get("integralInfo") or {}

        # 首页汇总：totalAmount / commonFlow 只是“首页显示提示”，showField=1 时给
        # 已用、=2 时给剩余，total/balance 常见为 "0"。真实总量只有 flowList
        # 与 userFluxPackage 才有，所以这里只把它们当作回退值。
        total_amount = flow_info.get("totalAmount") or {}
        amount_use = int(self._to_number(total_amount.get("used")))
        amount_balance = int(self._to_number(total_amount.get("balance")))
        amount_total = int(self._to_number(total_amount.get("total")))
        flow_over = int(self._to_number(total_amount.get("over")))

        common_flow = flow_info.get("commonFlow") or {}
        common_use_fallback = int(self._to_number(common_flow.get("used")))
        common_balance_fallback = int(self._to_number(common_flow.get("balance")))
        common_over = int(self._to_number(common_flow.get("over")))

        # 首页逐条列出的流量（“已用 / 剩余 / 共”）
        flow_items = self.parse_flow_list(flow_info.get("flowList") or [])
        list_use = sum(item["use"] for item in flow_items)
        list_balance = sum(item["balance"] for item in flow_items)
        list_total = sum(item["total"] for item in flow_items)

        if list_total > 0:
            flow_use = max(list_use, amount_use)
            flow_total = max(list_total, flow_use)
        else:
            flow_use = amount_use
            flow_total = max(amount_total, amount_use + amount_balance, flow_use)
        flow_balance = max(flow_total - flow_use, 0)

        # 流量包明细（userFluxPackage）：通用/专用拆分的最权威来源
        packages, groups, plan_name, share_tip = self.parse_flux_packages(flux_data)
        common_group = next((g for g in groups.values() if g["kind"] == "通用"), None)
        special_group = next((g for g in groups.values() if g["kind"] != "通用"), None)

        if common_group and common_group["total"] > 0:
            common_use = common_group["use"]
            common_balance = common_group["balance"]
            common_total = common_group["total"]
        else:
            common_use = common_use_fallback
            common_balance = common_balance_fallback
            common_total = common_use + common_balance

        if special_group and special_group["total"] > 0:
            special_use = special_group["use"]
            special_balance = special_group["balance"]
            special_total = special_group["total"]
        else:
            special_amount = flow_info.get("specialAmount") or {}
            special_use = int(self._to_number(special_amount.get("used")))
            special_balance = int(self._to_number(special_amount.get("balance")))
            special_total = special_use + special_balance

        # 语音通话
        voice_data_info = voice_info.get("voiceDataInfo") or {}
        voice_usage = int(voice_data_info.get("used") or 0)
        voice_balance = int(voice_data_info.get("balance") or 0)
        voice_total = int(voice_data_info.get("total") or 0)
        
        # 余额处理，包含欠费逻辑
        balance_info = data.get("balanceInfo") or {}
        balance_info_data = balance_info.get("indexBalanceDataInfo") or {}
        balance_str = balance_info_data.get("balance", "0.00")
        arrear_str = balance_info_data.get("arrear", "0.00")

        # 将字符串转换为浮点数进行比较和计算
        balance_float = self._to_number(balance_str)
        arrear_float = self._to_number(arrear_str)

        # 逻辑：如果余额为0且有欠费，则将余额设置为负的欠费值
        if balance_float == 0.00 and arrear_float > 0.00:
            balance = -int(round(arrear_float * 100))
        else:
            balance = int(round(balance_float * 100))

        phone_bill_region = balance_info.get("phoneBillRegion") or {}
        if not isinstance(phone_bill_region, dict):
            phone_bill_region = {}
        current_month_cost_str = str(phone_bill_region.get("subTitleHh") or "0元").replace('元', '')
        try:
            current_month_cost = int(round(float(current_month_cost_str) * 100))
        except ValueError:
            current_month_cost = 0 # 转换失败则设为0
        
        # 积分
        points = int(self._to_number(integral_info.get("integral")))

        summary = {
            "phonenum": phonenum,
            "balance": balance,
            "currentMonthCost": current_month_cost, # 将本月消费添加到 summary
            "voiceUsage": voice_usage,
            "voiceBalance": voice_balance,
            "voiceTotal": voice_total,
            "flowUse": flow_use,
            "flowTotal": flow_total,
            "flowBalance": flow_balance,
            "flowOver": flow_over,
            "commonUse": common_use,
            "commonTotal": common_total,
            "commonBalance": common_balance,
            "commonOver": common_over,
            "specialUse": special_use,
            "specialTotal": special_total,
            "points": points, 
            "createTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "flowItems": flow_items,
            "packages": packages,
            "groups": list(groups.values()),
            "planName": plan_name,
            "shareTip": share_tip,
        }
        return summary

    def convert_flow(self, size_str, target_unit="KB", decimal=0):
        unit_dict = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        if not size_str:
            return 0
        if isinstance(size_str, str):
            size, unit = float(size_str[:-2]), size_str[-2:]
        elif isinstance(size_str, (int, float)):
            size, unit = size_str, "KB"
        if unit in unit_dict or target_unit in unit_dict:
            return (
                int(size * unit_dict[unit] / unit_dict[target_unit])
                if decimal == 0
                else round(size * unit_dict[unit] / unit_dict[target_unit], decimal)
            )
        else:
            raise ValueError("Invalid unit")
