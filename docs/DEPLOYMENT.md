# 배포 가이드 — building-energy-3d

## 도메인 & DNS (2026-03-28 완료)

| 항목 | 값 |
|------|-----|
| 도메인 | `building-energy.xyz` |
| 레지스트라 | Namecheap (계정: jukim@smu.ac.kr) |
| DNS 관리 | Cloudflare (무료 플랜) |
| Cloudflare 계정 | jukim@smu.ac.kr |
| Zone ID | `90ba051f654c3e78c428546f8db402a9` |
| Account ID | `a1c4da17ce70ba1ad9dad2fd71b67d4d` |

### Cloudflare 네임서버 (Namecheap에 등록 완료)

```
NS1: jarred.ns.cloudflare.com
NS2: vera.ns.cloudflare.com
```

- Namecheap Custom DNS 설정 완료 (2026-03-28)
- DNS 전파 대기 중 (최대 24시간)
- Cloudflare 대시보드에서 상태가 **Active**로 바뀌면 다음 단계 진행

---

## 서버 구성 (예정)

| 항목 | 내용 |
|------|------|
| 서버 | Linux 서버 (RTX 5090 장착) |
| 외부 노출 | Cloudflare Tunnel (cloudflared) — 공유기 포트 오픈 불필요 |
| 리버스 프록시 | Nginx |

### Cloudflare Tunnel 설정 순서 (DNS Active 후 진행)

```bash
# 1. Linux 서버에서 cloudflared 설치
curl -L --output cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# 2. Cloudflare 로그인
cloudflared tunnel login

# 3. 터널 생성
cloudflared tunnel create building-energy

# 4. 터널 설정 파일 작성 (~/.cloudflared/config.yml)
# tunnel: <TUNNEL_ID>
# credentials-file: /root/.cloudflared/<TUNNEL_ID>.json
# ingress:
#   - hostname: building-energy.xyz
#     service: http://localhost:8000
#   - hostname: www.building-energy.xyz
#     service: http://localhost:8000
#   - service: http_status:404

# 5. DNS 레코드 등록 (Cloudflare에 CNAME 자동 생성)
cloudflared tunnel route dns building-energy building-energy.xyz

# 6. 터널 실행 (데몬)
sudo cloudflared service install
sudo systemctl start cloudflared
sudo systemctl enable cloudflared
```

### Nginx 설정 (로컬 리버스 프록시)

```nginx
# /etc/nginx/sites-available/building-energy
server {
    listen 8000;
    server_name localhost;

    # FastAPI (메인 API)
    location /api/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # 프론트엔드 정적 파일
    location / {
        root /path/to/building-energy-3d/frontend/dist;
        try_files $uri $uri/ /index.html;
    }
}
```

---

## VWorld API 키 도메인 등록 (미완료)

VWorld API 키에 `building-energy.xyz` 도메인을 허용 목록에 추가해야 함.
- VWorld 콘솔에서 API 키 관리 → 허용 도메인 추가

---

## 체크리스트

- [x] 도메인 구매 (building-energy.xyz, Namecheap, 2026-03-28)
- [x] Cloudflare 사이트 추가
- [x] Namecheap 네임서버 → Cloudflare 변경 (2026-03-28)
- [ ] Cloudflare DNS Active 확인 (전파 대기 중)
- [ ] Linux 서버에 cloudflared 설치 및 터널 설정
- [ ] Nginx 설정
- [ ] VWorld API 키 도메인 등록
- [ ] Docker Compose 서비스 실행 확인
- [ ] HTTPS 인증서 확인 (Cloudflare 자동 발급)
