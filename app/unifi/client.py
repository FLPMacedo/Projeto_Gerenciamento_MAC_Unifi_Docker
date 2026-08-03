"""Cliente da API do UniFi OS (Cloud Key / Dream Machine / etc.).

Diferente do controller legado (porta 8443, /api/...), o UniFi OS expoe o
login em /api/auth/login e faz proxy da API do Network em
/proxy/network/api/s/<site>/...  As escritas exigem o cabecalho X-CSRF-Token.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import requests
import urllib3


class UnifiError(Exception):
    """Erro retornado pela API ou por falha de comunicacao com o controller."""


# Limite de entradas do MAC-filter por WLAN no UniFi.
MAC_FILTER_CAP = 512


class UnifiClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        site: str = "default",
        verify_ssl: bool = False,
        timeout: int = 15,
    ) -> None:
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.site = site
        self.timeout = timeout

        self.session = requests.Session()
        self.session.verify = verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._csrf: Optional[str] = None
        self._logged_in = False

    # ------------------------------------------------------------------ infra
    def _net_url(self, path: str) -> str:
        """Monta uma URL da API do Network (proxied) para o site atual."""
        return f"{self.host}/proxy/network/api/s/{self.site}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._csrf:
            h["X-CSRF-Token"] = self._csrf
        return h

    def _capture_csrf(self, resp: requests.Response) -> None:
        # UniFi OS devolve o token CSRF no header e/ou embutido no JWT do cookie TOKEN.
        token = resp.headers.get("X-CSRF-Token") or resp.headers.get("x-csrf-token")
        if token:
            self._csrf = token
            return
        jwt = self.session.cookies.get("TOKEN")
        if jwt:
            try:
                import base64
                import json

                payload = jwt.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                data = json.loads(base64.urlsafe_b64decode(payload))
                if data.get("csrfToken"):
                    self._csrf = data["csrfToken"]
            except Exception:
                pass

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        # Trava de seguranca: so permitimos LEITURA (GET) e EDICAO DA ALLOW-LIST
        # (PUT em rest/wlanconf, usado por add/remover unitario). POST/DELETE e
        # qualquer outra escrita (block-sta, etc.) continuam bloqueados.
        if method.upper() not in ("GET", "PUT"):
            raise UnifiError(
                "Operacao nao permitida: apenas leitura e edicao da allow-list."
            )
        try:
            resp = self.session.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise UnifiError(f"Falha de conexao com {url}: {exc}") from exc

        self._capture_csrf(resp)

        if resp.status_code == 401 and self._logged_in:
            # Sessao expirou: refaz login uma vez e repete a chamada.
            self._logged_in = False
            self.login()
            resp = self.session.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
            self._capture_csrf(resp)

        if not resp.ok:
            raise UnifiError(f"HTTP {resp.status_code} em {url}: {resp.text[:300]}")

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    @staticmethod
    def _data(payload: Any) -> list[dict]:
        """Extrai a lista 'data' do envelope padrao da API do Network."""
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload if isinstance(payload, list) else []

    @staticmethod
    def normalize_mac(mac: str) -> str:
        clean = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
        if len(clean) != 12:
            raise UnifiError(f"MAC invalido: {mac!r}")
        return ":".join(clean[i : i + 2] for i in range(0, 12, 2))

    # ----------------------------------------------------------------- sessao
    def login(self) -> None:
        url = f"{self.host}/api/auth/login"
        try:
            resp = self.session.post(
                url,
                json={"username": self.username, "password": self.password},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise UnifiError(f"Falha de conexao ao logar em {url}: {exc}") from exc

        if not resp.ok:
            raise UnifiError(f"Login falhou (HTTP {resp.status_code}): {resp.text[:300]}")

        self._capture_csrf(resp)
        self._logged_in = True

    def logout(self) -> None:
        if self._logged_in:
            try:
                self.session.post(
                    f"{self.host}/api/auth/logout",
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except requests.RequestException:
                pass
            self._logged_in = False

    def __enter__(self) -> "UnifiClient":
        self.login()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.logout()

    # ------------------------------------------------------------- consultas
    def get_clients(self) -> list[dict]:
        """Clientes atualmente conectados (online)."""
        return self._data(self._request("GET", self._net_url("stat/sta")))

    def get_known_clients(self) -> list[dict]:
        """Todos os clientes ja vistos/configurados (inclui offline e bloqueados)."""
        return self._data(self._request("GET", self._net_url("rest/user")))

    def find_client(self, mac: str) -> Optional[dict]:
        mac = self.normalize_mac(mac)
        for c in self.get_known_clients():
            if c.get("mac", "").lower() == mac:
                return c
        return None

    # ------------------------------------------------------------------ sites
    def get_sites(self) -> list[dict]:
        """Lista todos os sites do controller (id interno + nome amigavel)."""
        data = self._data(self._request("GET", f"{self.host}/proxy/network/api/self/sites"))
        out = []
        for s in data:
            out.append(
                {
                    "id": s.get("name"),          # id interno usado nas URLs
                    "desc": s.get("desc") or s.get("name"),
                    "num_ap": s.get("num_ap"),
                }
            )
        out.sort(key=lambda x: x["desc"].lower())
        return out

    # ------------------------------------------------------------------ wlans
    def get_wlans(self) -> list[dict]:
        return self._data(self._request("GET", self._net_url("rest/wlanconf")))

    def get_wlan(self, wlan_id: str) -> Optional[dict]:
        data = self._data(self._request("GET", self._net_url(f"rest/wlanconf/{wlan_id}")))
        return data[0] if data else None

    def get_mobile_wlans(self) -> list[dict]:
        """WLANs cuja finalidade e a rede mobile (nome contem MOBILE)."""
        return [w for w in self.get_wlans() if "MOBILE" in (w.get("name") or "").upper()]

    # --------------------------------------------------------------- historico
    def get_all_users(self) -> list[dict]:
        """Todos os clientes ja vistos (online + offline) com last_seen/first_seen."""
        return self._data(
            self._request(
                "GET",
                self._net_url("stat/alluser"),
                params={"type": "all", "conn": "all"},
            )
        )

    def get_admin_activity(self, site: str | None = None, page: int = 0,
                           page_size: int = 200) -> tuple[list, int]:
        """Log NATIVO de atividade administrativa da UniFi (leitura via POST).
        Retorna (itens, total). Usa session.post (consulta, nao escrita de config)."""
        site = site or self.site
        url = (f"{self.host}/proxy/network/v2/api/site/{site}"
               f"/system-log/admin-activity")
        try:
            resp = self.session.post(
                url, headers=self._headers(),
                json={"pageNumber": page, "pageSize": page_size},
                timeout=self.timeout)
        except requests.RequestException as exc:
            raise UnifiError(f"Falha ao ler log da UniFi: {exc}") from exc
        self._capture_csrf(resp)
        if not resp.ok:
            raise UnifiError(f"HTTP {resp.status_code} no admin-activity: {resp.text[:200]}")
        j = resp.json()
        return j.get("data", []), j.get("total_element_count", 0)

    # ----------------------------------------- edicao da allow-list (unitaria)
    def set_mac_filter_list(self, wlan_id: str, macs: list[str]) -> Any:
        norm = [self.normalize_mac(m) for m in macs]
        return self._request(
            "PUT", self._net_url(f"rest/wlanconf/{wlan_id}"),
            json={"mac_filter_list": norm})

    def add_mac_to_wlan(self, wlan_id: str, mac: str) -> dict:
        """Adiciona 1 MAC a allow-list. Erro se cheia (sem folga, 512)."""
        mac = self.normalize_mac(mac)
        wlan = self.get_wlan(wlan_id)
        if not wlan:
            raise UnifiError(f"WLAN {wlan_id} nao encontrada.")
        lst = [m.lower() for m in (wlan.get("mac_filter_list") or [])]
        if mac in lst:
            return {"changed": False, "reason": "ja_cadastrado", "count": len(lst)}
        if len(lst) >= MAC_FILTER_CAP:
            raise UnifiError(
                f"Sem folga: lista cheia ({len(lst)}/{MAC_FILTER_CAP}). "
                "Remova um MAC sem uso antes de adicionar.")
        lst.append(mac)
        self.set_mac_filter_list(wlan_id, lst)
        return {"changed": True, "count": len(lst)}

    def remove_mac_from_wlan(self, wlan_id: str, mac: str) -> dict:
        """Remove 1 MAC da allow-list (libera 1 vaga)."""
        mac = self.normalize_mac(mac)
        wlan = self.get_wlan(wlan_id)
        if not wlan:
            raise UnifiError(f"WLAN {wlan_id} nao encontrada.")
        lst = [m.lower() for m in (wlan.get("mac_filter_list") or [])]
        if mac not in lst:
            return {"changed": False, "reason": "nao_estava", "count": len(lst)}
        lst = [m for m in lst if m != mac]
        self.set_mac_filter_list(wlan_id, lst)
        return {"changed": True, "count": len(lst)}
