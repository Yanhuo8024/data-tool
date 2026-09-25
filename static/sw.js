const CACHE_NAME = 'datatool-v9';
const STATIC_ASSETS = [
  '/static/style.css',
  '/static/canvas.css',
  '/static/chartStore.js',
  '/static/linkedKLine.js',
  '/static/mobile-touch.js',
  '/static/js/core.js',
  '/static/js/data-analysis.js',
  '/static/js/did.js',
  '/static/js/finance.js',
  '/static/js/finance-sync.js',
  '/static/js/ml.js',
  '/static/js/fixedincome.js',
  '/static/js/fund.js',
  '/static/js/system.js',
  '/static/js/version-check.js',
  '/static/js/lineage.js',
  '/static/favicon.svg',
  '/static/manifest.json',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => 
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  // HTML 页面：网络优先（避免 Service Worker 缓存旧页面导致"界面还是旧的"）
  if (url.pathname === '/' || url.pathname.endsWith('.html')) {
    event.respondWith(
      fetch(event.request).then(response => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone)).catch(() => {});
        return response;
      }).catch(() => caches.match(event.request))
    );
    return;
  }
  // 静态资源：stale-while-revalidate（带 ?v= 版本号，更新靠版本号变化）
  event.respondWith(
    caches.match(event.request).then(cached => {
      const fetchPromise = fetch(event.request).then(response => {
        if (response.ok && response.type === 'basic') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached);
      return cached || fetchPromise;
    })
  );
});
