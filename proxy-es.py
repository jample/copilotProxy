import asyncio
from mitmproxy import http,ctx,connection,proxy
from elasticsearch import Elasticsearch
from datetime import datetime
import base64
import re
import os
import json

import functools

# 初始化Elasticsearch客户端

ELASTICSEARCH_URL = "https://xxx:9200/"
#ELASTICSEARCH_URL = "http://es-test-es-http-ext:9200/"
ELASTICSEARCH_USERNAME = "admin"
ELASTICSEARCH_PASSWORD = "Jessie@123"

es = Elasticsearch(
    [ELASTICSEARCH_URL],
    # use_ssl=False,
    verify_certs=False,
    http_auth=(ELASTICSEARCH_USERNAME, ELASTICSEARCH_PASSWORD),
)

auth_whitelist_url = [
    "api.github.com.*",
    "api.enterprise.githubcopilot.com.*",
    "api.busniess.githubcopilot.com.*",
]

allowed_patterns = [
    #  ".*",
     "https://.*",
     "https://github.com/$",
     "https://github.com/login.*",
     "https://vscode.dev/redirect.*",
     "https://github.com/settings/two_factor_checkup.*",
     "https://github.com/favicon.ico",
     "https://github.com/session",
     "https://github.com/sessions.*",
     "https://github.githubassets.com/assets.*",
     "https://api.github.com/user",
     "https://education.github.com/api/user",
     "https://api.github.com/copilot_internal/v2/token",
     "https://api.github.com/copilot_internal/notification",
     "https://default.exp-tas.com",
     "https://default.exp-tas.com/vscode/ab",
     "https://copilot-telemetry.githubusercontent.com/telemetry",
     "https://copilot-proxy.githubusercontent.com.*",
     "https://api.github.com/applications/[0-9a-fA-F]+/token",
     "https://api.githubcopilot.com/chat/completions.*",
      "https://.*\\.githubcopilot\\.com.*",
     "https://.*\\.business\\.githubcopilot\\.com.*",
     "https://.*\\.enterprise\\.githubcopilot\\.com.*",
]

# 身份验证函数
# def authenticate(username, password):
#     # 在这里实现你的身份验证逻辑
#     # 返回True表示验证通过，False表示验证失败
#     return username == password
def is_url_allowed(url: str, allowed_patterns) -> bool:
    for pattern in allowed_patterns:
        if re.match(pattern, url):
            return True
    return False

class AuthProxy:
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.proxy_authorizations = {} 
        self.credentials = self.load_credentials("creds.txt")

    def load_credentials(self, file_path):
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Credentials file '{file_path}' not found")
        creds = {}
        with open(file_path, "r") as f:
            for line in f:
                username, password = line.strip().split(",")
                creds[username] = password
        return creds

    def http_connect(self, flow: http.HTTPFlow):
        proxy_auth = flow.request.headers.get("Proxy-Authorization", "")
        url = flow.request.pretty_url
        # 如果没有代理授权头，返回401
        if not proxy_auth and not is_url_allowed(url, auth_whitelist_url):
            ctx.log.info("Proxy-Authorization: 401 failed " + url)
            flow.response = http.Response.make(401)

        ctx.log.info("Proxy-Authorization: " + proxy_auth.strip())
        if proxy_auth.strip() == "" :
            self.proxy_authorizations[(flow.client_conn.id)] = ""
            return
        auth_type, auth_string = proxy_auth.split(" ", 1)
        auth_string = base64.b64decode(auth_string).decode("utf-8")
        username, password = auth_string.split(":")
        ctx.log.info("User: " + username + " Password: " + password)

        if username in self.credentials:
            # If the username exists, check if the password is correct
            if self.credentials[username] != password:
                ctx.log.info("User: " + username + " attempted to log in with an incorrect password.")
                flow.response = http.Response.make(401)
                return
        else:
            # If the username does not exist, log the event and return a 401 response
            ctx.log.info("Username: " + username + " does not exist.")
            flow.response = http.Response.make(401)
            return
    

        ctx.log.info("Authenticated: " + flow.client_conn.address[0])
        self.proxy_authorizations[(flow.client_conn.address[0])] = username
    
    
    def request(self, flow: http.HTTPFlow):
        if not is_url_allowed(flow.request.url, allowed_patterns):
            flow.response = http.Response.make(403, b"Forbidden", {"Content-Type": "text/html"})
        # if flow.request.pretty_host.endswith("baidu.com"):
        # # 修改请求的目的地址
        #     flow.request.host = "52.253.115.203"
        #     flow.request.scheme = "http"
        #     flow.request.port = 8015

    def response(self, flow: http.HTTPFlow):
        # 异步将请求和响应存储到Elasticsearch
        ctx.log.info("response: " + flow.request.url)
        asyncio.ensure_future(self.save_to_elasticsearch(flow))

    async def split_jsons(self, json_string):
        json_objects = []
        depth = 0
        start_index = 0
        for i, char in enumerate(json_string):
            if char == '{':
                if depth == 0:
                    start_index = i
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    end_index = i + 1
                    try:
                        json_obj = json.loads(json_string[start_index:end_index])
                        json_objects.append(json_obj)
                    except json.JSONDecodeError as e:
                        print(f"Error decoding JSON: {e}")
        return json_objects

    async def save_to_elasticsearch(self, flow: http.HTTPFlow):
        ctx.log.info("url to es: " + flow.request.url)
        if "complet"  in flow.request.url or "telemetry"  in flow.request.url:
            
            username = self.proxy_authorizations.get(flow.client_conn.address[0])
            timeconsumed = round((flow.response.timestamp_end - flow.request.timestamp_start) * 1000, 2)
            timeconsumed_str = f"{timeconsumed}ms"  # Add "ms" to the end of the timeconsumed string
            
            ctx.log.info(username + ":\t consumed time: " + timeconsumed_str + str(flow.request.headers.get("x-request-id")))
            # 将请求和响应存储到Elasticsearch
            doc = {
                'user': username,
                "timestamp": datetime.utcnow().isoformat(),
                "proxy-time-consumed": timeconsumed_str,  # Use the modified timeconsumed string
                'request': {
                    'url': flow.request.url,
                    'method': flow.request.method,
                    'headers': dict(flow.request.headers),
                    'content': flow.request.content.decode('utf-8', 'ignore'),
                },
                'response': {
                    'status_code': flow.response.status_code,
                    'headers': dict(flow.response.headers),
                    'content': flow.response.content.decode('utf-8', 'ignore'),
                }
            }
            if "complet"  in flow.request.url:
                index_func = functools.partial(es.index, index='mitmproxy', body=doc)
                await self.loop.run_in_executor(None, index_func)
            else:
                request_content = flow.request.content.decode('utf-8', 'ignore')
                json_objects = await self.split_jsons(request_content)
                # print(json_objects)
                for obj in json_objects:
                    ctx.log.info("obj: ===" + str(obj))
                    baseDataName = obj.get("data").get("baseData").get("name")
                    accepted_numLines = 0
                    accepted_charLens = 0
                    shown_numLines = 0
                    shown_charLens = 0
                    ctx.log.info("baseDataName: === " + baseDataName)
                    ctx.log.info("obj: ===" + str(obj))
                    if "hown" in baseDataName or "accepted" in baseDataName:
                        if "hown" in baseDataName:
                            shown_numLines = obj.get("data").get("baseData").get("measurements").get("numLines")
                            shown_charLens = obj.get("data").get("baseData").get("measurements").get("compCharLen")
                        else: 
                            accepted_numLines = obj.get("data").get("baseData").get("measurements").get("numLines")
                            accepted_charLens = obj.get("data").get("baseData").get("measurements").get("compCharLen")
                        doc = {
                            'user': username,
                            "timestamp": datetime.utcnow().isoformat(),
                            "proxy-time-consumed": timeconsumed_str,  # Use the modified timeconsumed string
                            'request': {
                                'url': flow.request.url,
                                'baseData': baseDataName,
                                'accepted_numLines': accepted_numLines,
                                'shown_numLines': shown_numLines,
                                'accepted_charLens': accepted_charLens,
                                'shown_charLens': shown_charLens,
                                'language': obj.get("data").get("baseData").get("properties").get("languageId"),
                                'editor': obj.get("data").get("baseData").get("properties").get("editor_version").split("/")[0],
                                'editor_version': obj.get("data").get("baseData").get("properties").get("editor_version").split("/")[1],
                                'copilot-ext-version': obj.get("data").get("baseData").get("properties").get("common_extversion"),
                            },
                            'response': {
                                'status_code': flow.response.status_code,
                                'content': flow.response.content.decode('utf-8', 'ignore'),
                            }
                        }
                        index_func = functools.partial(es.index, index='telemetry', body=doc)
                        await self.loop.run_in_executor(None, index_func)

# 添加插件
addons = [
    AuthProxy()
]
