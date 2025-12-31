import re
import base64
import urllib.parse
from typing import List, Dict
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp

# ========== 1. 配置映射类（从配置文件读取参数） ==========
@dataclass
class MagnetConfig:
    """磁力搜索配置类"""
    base_url: str          # 站点基础地址
    search_path: str       # 搜索接口路径
    max_results: int       # 最大返回结果数
    page_size: int         # 单页展示数量
    request_timeout: int   # 请求超时时间（秒）
    captcha_cookies: Dict[str, str] = None  # 验证Cookie（固定值）

    def __post_init__(self):
        # 初始化固定验证Cookie
        self.captcha_cookies = {
            "sssfwz2": "qwsdsddsdsdse",
            "aywcUid": "lwgkvwDiYQ_20211009155217"
        }
        # 处理base_url结尾的/（统一格式：不带结尾/）
        if self.base_url.endswith("/"):
            self.base_url = self.base_url.rstrip("/")
        # 处理search_path开头的/（统一格式：带开头/）
        if not self.search_path.startswith("/"):
            self.search_path = f"/{self.search_path}"

# ========== 2. 核心工具类 ==========
class MagnetUtils:
    @staticmethod
    def decrypt_base64(encrypted_str: str) -> str:
        """Base64解密"""
        try:
            encrypted_str = encrypted_str.ljust(len(encrypted_str) + (4 - len(encrypted_str) % 4) % 4, '=')
            decoded = base64.b64decode(encrypted_str).decode('utf-8', errors='ignore')
            return urllib.parse.unquote(decoded)
        except Exception as e:
            logger.warning(f"Base64解密失败：{str(e)}")
            return ""

    @staticmethod
    def get_full_url(base_url: str, relative_url: str) -> str:
        """拼接完整URL"""
        if relative_url.startswith("http"):
            return relative_url
        if relative_url.startswith("./"):
            return f"{base_url}/{relative_url[2:]}"
        if relative_url.startswith("/"):
            return f"{base_url}{relative_url}"
        return f"{base_url}/{relative_url}"

# ========== 3. 核心搜索服务 ==========
class MagnetSearchService:
    def __init__(self, config: MagnetConfig):
        self.config = config
        self.client: httpx.AsyncClient = None

    async def _init_client(self):
        """初始化客户端：使用配置文件的超时时间"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": self.config.base_url,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": self.config.base_url
        }

        self.client = httpx.AsyncClient(
            headers=headers,
            cookies=self.config.captcha_cookies,
            timeout=self.config.request_timeout,  # 从配置读取超时时间
            follow_redirects=False,              # 关闭自动重定向
            verify=False
        )

    async def search(self, keyword: str) -> List[str]:
        """搜索逻辑：使用配置文件的站点/接口/结果数"""
        await self._init_client()
        results = []

        try:
            # ========== 构造搜索URL（从配置读取） ==========
            search_url = f"{self.config.base_url}{self.config.search_path}?name={urllib.parse.quote(keyword)}"
            logger.debug(f"GET请求：{search_url}")
            
            # 发起请求
            response = await self.client.get(search_url)
            logger.debug(f"响应状态码：{response.status_code}")
            
            # 提取原始响应
            raw_html = response.text

            # ========== 解密原始响应 ==========
            encrypt_match = re.search(r"window\.atob\('([^']+)'", raw_html)
            if not encrypt_match:
                logger.warning(f"未找到window.atob加密串")
                return []
            
            decrypted_html = MagnetUtils.decrypt_base64(encrypt_match.group(1))


            # ========== 提取xq.php链接（使用配置） ==========
            soup = BeautifulSoup(decrypted_html, "lxml")
            result_container = soup.find("ul", id="Search_list_wrapper")
            if not result_container:
                logger.warning(f"解密后仍无搜索结果容器")
                return []

            detail_links = []
            processed_urls = set()
            # 遍历结果：最多取配置的max_results条
            for idx, li in enumerate(result_container.find_all("li")):
                if idx >= self.config.max_results:  # 从配置读取最大结果数
                    break
                if li.find("ul", class_="pagination"):
                    continue

                link_tag = li.find("a", href=re.compile(r"xq\.php\?key="))
                if not link_tag:
                    continue

                full_url = MagnetUtils.get_full_url(self.config.base_url, link_tag.get("href"))
                if full_url in processed_urls:
                    continue
                processed_urls.add(full_url)

                # 提取基础信息
                title = link_tag.text.strip() or f"搜索结果{idx+1}"
                size = re.search(r"文件大小：([0-9.]+ [GMK]B)", li.text)
                size = size.group(1).strip() if size else "未知大小"
                create_time = re.search(r"创建时间：(\d{4}-\d{2}-\d{2})", li.text)
                create_time = create_time.group(1).strip() if create_time else "未知时间"

                detail_links.append({
                    "url": full_url,
                    "title": title,
                    "size": size,
                    "create_time": create_time
                })

            if not detail_links:
                return []

            # ========== 解析详情页 ==========
            for link in detail_links:
                try:
                    detail_resp = await self.client.get(link["url"], follow_redirects=False)
                    detail_raw = detail_resp.text

                    # 解密详情页
                    detail_encrypt = re.search(r"window\.atob\('([^']+)'", detail_raw)
                    detail_html = detail_raw
                    if detail_encrypt:
                        detail_html = MagnetUtils.decrypt_base64(detail_encrypt.group(1))

                    # 提取磁力链接
                    magnet_link = None
                    magnet_a = soup.find("a", href=re.compile(r"magnet:\?xt=urn:btih:"))
                    if magnet_a:
                        magnet_link = magnet_a.get("href").strip()
                    if not magnet_link:
                        magnet_match = re.search(r"magnet:\?xt=urn:btih:[a-fA-F0-9]{40,}[^\"']*", detail_html)
                        if magnet_match:
                            magnet_link = magnet_match.group().strip()

                    # 构造结果
                    results.append(
                        f"标题：{link['title']}\n"
                        f"磁力链接：{magnet_link or '未提取到'}\n"
                        f"文件大小：{link['size']}\n"
                        f"收录时间：{link['create_time']}"
                    )
                except Exception as e:
                    results.append(f"标题：{link['title']}\n解析失败：{str(e)[:30]}\n文件大小：{link['size']}")

        except Exception as e:
            logger.error(f"搜索异常：{str(e)}")
            results = [f"搜索失败：{str(e)[:50]}"]
        finally:
            if self.client:
                await self.client.aclose()

        return results

# ========== 4. 插件主类 ==========
@register(
    "astrbot_plugin_BitTorrent",
    "NightDust981989",
    "BitTorrent磁力搜索",
    "1.0.0",
    "https://github.com/NightDust981989/astrbot_plugin_BitTorrent"
)
class MagnetSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig, **kwargs):
        super().__init__(context)
        # ========== 从插件配置文件读取参数 ==========
        self._config_store = config
        self.search_config = self._build_config()
        self.search_service = MagnetSearchService(self.search_config)
        logger.info(
            f"磁力搜索插件初始化完成，使用站点：{self.search_config.base_url}{self.search_config.search_path}"
        )

    @staticmethod
    def _coerce_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _get_plugin_config(self) -> Dict:
        plugin_config = None
        try:
            plugin_config = self._config_store.get("magnet_search", None)
        except Exception:
            plugin_config = None
        if isinstance(plugin_config, dict):
            return plugin_config
        if hasattr(self._config_store, "get"):
            return self._config_store
        return {}

    def _build_config(self) -> MagnetConfig:
        plugin_config = self._get_plugin_config()
        base_url = plugin_config.get("base_url", "https://clg2.clgapp1.xyz")
        search_path = plugin_config.get("search_path", "/cllj.php")
        max_results = self._coerce_int(plugin_config.get("max_results", 20), 20)
        page_size = self._coerce_int(plugin_config.get("page_size", 5), 5)
        request_timeout = self._coerce_int(plugin_config.get("request_timeout", 15), 15)
        return MagnetConfig(
            base_url=base_url,
            search_path=search_path,
            max_results=max_results,
            page_size=page_size,
            request_timeout=request_timeout
        )

    def _refresh_config(self) -> None:
        self.search_config = self._build_config()
        self.search_service = MagnetSearchService(self.search_config)

    @filter.command("bt")
    async def magnet_search_handler(self, event: AstrMessageEvent):
        """
        磁力链接搜索指令
        使用方式：bt [关键词]
        示例：bt 安达与岛村
        """
        message = event.message_str.strip()
        args = message.split()

        self._refresh_config()
    
        # 初始化消息链
        chain = []
    
        if len(args) < 2 or args[0] != "bt":
            # 提示整合到chain
            chain.append(Comp.Plain("用法：bt [关键词] [-p 页码]\n示例：bt 安达与岛村 -p 2"))
            yield event.chain_result(chain)
            return

        page = 1
        args_after_cmd = args[1:]
        if "-p" in args_after_cmd:
            p_idx = args_after_cmd.index("-p")
            if p_idx + 1 >= len(args_after_cmd) or not args_after_cmd[p_idx + 1].isdigit():
                chain.append(Comp.Plain("页码参数无效，请使用：bt [关键词] -p [页码]"))
                yield event.chain_result(chain)
                return
            page = int(args_after_cmd[p_idx + 1])
            keyword_parts = args_after_cmd[:p_idx] + args_after_cmd[p_idx + 2:]
        elif "--page" in args_after_cmd:
            p_idx = args_after_cmd.index("--page")
            if p_idx + 1 >= len(args_after_cmd) or not args_after_cmd[p_idx + 1].isdigit():
                chain.append(Comp.Plain("页码参数无效，请使用：bt [关键词] --page [页码]"))
                yield event.chain_result(chain)
                return
            page = int(args_after_cmd[p_idx + 1])
            keyword_parts = args_after_cmd[:p_idx] + args_after_cmd[p_idx + 2:]
        else:
            keyword_parts = args_after_cmd

        if page < 1:
            chain.append(Comp.Plain("页码需为大于等于 1 的整数"))
            yield event.chain_result(chain)
            return

        if not keyword_parts:
            chain.append(Comp.Plain("用法：bt [关键词] [-p 页码]\n示例：bt 安达与岛村 -p 2"))
            yield event.chain_result(chain)
            return

        keyword = " ".join(keyword_parts)
        results = await self.search_service.search(keyword)
    
        if not results:
            # 无结果的chain
            chain.append(Comp.Plain("未找到相关磁力链接"))
        else:
            # 有结果时拼接完整内容
            page_size = (
                self.search_config.page_size
                if self.search_config.page_size and self.search_config.page_size > 0
                else len(results)
            )
            total_pages = (len(results) + page_size - 1) // page_size
            if page > total_pages:
                chain.append(Comp.Plain(f"页码超出范围，当前共有 {total_pages} 页"))
                yield event.chain_result(chain)
                return

            start = (page - 1) * page_size
            end = start + page_size
            page_results = results[start:end]
            chain.append(Comp.Plain(f"共找到 {len(results)} 条有效结果，当前第 {page}/{total_pages} 页："))
            for idx, res in enumerate(page_results, start + 1):
                chain.append(Comp.Plain(f"‎\n===== 结果 {idx} =====\n‎{res}"))
    
        # 返回完整的消息链
        yield event.chain_result(chain)
