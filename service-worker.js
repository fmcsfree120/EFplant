// EFplant Service Worker
// 強制 HTML 與 health.json 永遠從網路抓取，不走 PWA 快取
// 這是解決 iOS/Android PWA 快取舊頁面問題的根本方案

const SW_VER = 'efplant-sw-v34-kf1-classify';

self.addEventListener('install', function(e) {
    self.skipWaiting(); // 立即接管，不等舊 SW 結束
});

self.addEventListener('activate', function(e) {
    e.waitUntil(
        caches.keys().then(function(keys) {
            return Promise.all(
                keys.filter(function(k) { return k !== SW_VER; })
                    .map(function(k) { return caches.delete(k); })
            );
        }).then(function() {
            return clients.claim(); // 立即控制所有頁面
        })
    );
});

self.addEventListener('fetch', function(e) {
    var url = new URL(e.request.url);
    var path = url.pathname;

    // HTML 主頁面、chart.html、health.json：永遠從網路抓，不快取
    var isMainPage = (e.request.mode === 'navigate' ||
                      path.endsWith('/') ||
                      path.endsWith('/index.html') ||
                      path.endsWith('index.html') ||
                      path.endsWith('chart.html'));
    var isHealthJson = path.endsWith('health.json');
    var isDataEnc    = path.endsWith('data.enc');

    if (isMainPage || isHealthJson || isDataEnc) {
        e.respondWith(
            fetch(e.request, { cache: 'no-store' })
                .catch(function() {
                    // 網路失敗時才退回快取（離線保底）
                    return caches.match(e.request);
                })
        );
        return;
    }

    // 其他靜態資源（CryptoJS CDN 等）：正常快取加速
    e.respondWith(fetch(e.request));
});
