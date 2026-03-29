# 배포 가이드 — building-energy-3d

**최종 수정**: 2026-03-28 (Windows + Cloudflare Tunnel 배포 완료)

---

## 배포 현황 (2026-03-28 완료)

| 항목 | 상태 | 내용 |
|------|------|------|
| 도메인 | ✅ | `building-energy.xyz` (Namecheap, jukim@smu.ac.kr) |
| DNS | ✅ | Cloudflare Zone Active, NS 전파 완료 |
| Cloudflare Tunnel | ✅ | Tunnel ID: `39a65ef6-8e7d-4fc7-900a-771910f8b986` |
| Windows 서비스 | ✅ | `Cloudflared` — Automatic, Running |
| Docker Compose | ✅ | `restart: unless-stopped` 전 서비스 적용 |
| VWorld API 키 도메인 | ✅ | `building-energy.xyz` 등록 완료 |
| Mixed Content | ✅ | `upgrade-insecure-requests` CSP 적용 |

---

## 도메인 & DNS

| 항목 | 값 |
|------|-----|
| 도메인 | `building-energy.xyz` |
| 레지스트라 | Namecheap (jukim@smu.ac.kr) |
| DNS 관리 | Cloudflare (무료 플랜) |
| Cloudflare 계정 | jukim@smu.ac.kr |
| Zone ID | `90ba051f654c3e78c428546f8db402a9` |
| Account ID | `a1c4da17ce70ba1ad9dad2fd71b67d4d` |

### Cloudflare 네임서버

```
NS1: jarred.ns.cloudflare.com
NS2: vera.ns.cloudflare.com
```

---

## 서버 구성 (현재 — Windows RTX 4090)

| 항목 | 내용 |
|------|------|
| 서버 | Windows 11 (RTX 4090) |
| 컨테이너 | Docker Desktop + Docker Compose |
| 외부 노출 | Cloudflare Tunnel (cloudflared) — 포트 포워딩 불필요 |
| 자동 시작 | Docker Desktop 로그인 시 시작 + `restart: unless-stopped` |

---

## Cloudflare Tunnel 구성 (완료)

### 설치 경로

```
C:\Users\User\AppData\Local\cloudflared\cloudflared.exe  (v2026.3.0)
C:\Users\User\.cloudflared\cert.pem                      (Cloudflare 인증서)
C:\Users\User\.cloudflared\config.yml                    (터널 설정)
C:\Users\User\.cloudflared\39a65ef6-....json             (터널 자격증명)
```

### config.yml

```yaml
tunnel: 39a65ef6-8e7d-4fc7-900a-771910f8b986
credentials-file: C:\Users\User\.cloudflared\39a65ef6-8e7d-4fc7-900a-771910f8b986.json

ingress:
  - hostname: building-energy.xyz
    service: http://localhost:5173
  - hostname: www.building-energy.xyz
    service: http://localhost:5173
  - service: http_status:404
```

### Windows 서비스 관리

```powershell
# 상태 확인
Get-Service Cloudflared

# 재시작
Restart-Service Cloudflared  # 관리자 권한 필요

# 서비스 제거 (필요 시)
cloudflared service uninstall  # 관리자 권한 필요
```

### 터널 상태 확인

```bash
cloudflared tunnel info building-energy
cloudflared tunnel list
```

---

## Docker Compose 서비스

```bash
# 전체 시작
docker compose up -d

# 상태 확인
docker compose ps

# 로그 확인
docker compose logs -f api
docker compose logs -f frontend

# 재빌드 (소스 변경 후)
docker compose build api && docker compose up -d api
```

### 포트 매핑

| 서비스 | 내부 포트 | 호스트 포트 |
|--------|----------|-----------|
| frontend (Vite) | 5173 | 5173 → Cloudflare Tunnel |
| api (FastAPI) | 8000 | 8000 |
| db (PostGIS) | 5432 | 5434 |
| redis | 6379 | 6379 |

---

## 재부팅 시 자동 복구 흐름

1. Windows 시작
2. Docker Desktop 자동 시작 (설정 → General → Start Docker Desktop when you sign in to your computer)
3. `restart: unless-stopped` → 모든 컨테이너 자동 기동
4. `Cloudflared` Windows 서비스 자동 시작 → Tunnel 자동 연결

---

## 향후 이전 계획 (RTX 5090 Linux 서버)

```bash
# Linux 서버에서 cloudflared 설치
curl -L --output cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# 로그인 (기존 cert.pem 복사 또는 재로그인)
cloudflared tunnel login

# 기존 터널 재사용 (credentials JSON 복사)
# 또는 새 터널 생성: cloudflared tunnel create building-energy

# config.yml 동일하게 작성 (service 포트만 조정)
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

---

## 체크리스트

- [x] 도메인 구매 (building-energy.xyz, Namecheap, 2026-03-28)
- [x] Cloudflare 사이트 추가
- [x] Namecheap 네임서버 → Cloudflare 변경 (2026-03-28)
- [x] Cloudflare DNS Active 확인 (2026-03-28)
- [x] Windows에 cloudflared 설치 (v2026.3.0, 2026-03-28)
- [x] Cloudflare Tunnel 생성 (building-energy, 2026-03-28)
- [x] DNS CNAME 등록 (building-energy.xyz + www, 2026-03-28)
- [x] Windows 서비스 등록 (Cloudflared, Automatic, 2026-03-28)
- [x] Docker Compose restart: unless-stopped 적용 (2026-03-28)
- [x] VWorld API 키 도메인 등록 (building-energy.xyz, 2026-03-28)
- [x] Mixed Content 해결 (upgrade-insecure-requests, 2026-03-28)
- [x] HTTPS 인증서 (Cloudflare 자동 발급)
- [ ] Docker Desktop 로그인 시 자동 시작 설정 (수동 체크 필요)
- [ ] Linux 서버 이전 (RTX 5090, 시기 미정)
