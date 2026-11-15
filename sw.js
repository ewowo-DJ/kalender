// Keeps the app shell available offline; calendar data itself syncs through Firebase.
// Also shows the push notifications sent by scripts/notify.py (Firebase Cloud Messaging).
const CACHE = "calendar-v3";
const SHELL = ["./", "index.html", "config.js", "manifest.webmanifest", "icon-192.png", "icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // Network first, skipping the browser cache, so updates show up straight away; fall back to the cached copy when offline.
  e.respondWith(fetch(e.request, {cache: "no-store"}).then(r => { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return r; })
    .catch(() => caches.match(e.request).then(r => r || caches.match("index.html"))));
});

self.addEventListener("push", e => {
  let msg = {};
  try { msg = e.data ? e.data.json() : {}; } catch (err) { msg = {data: {title: "Onze kalender", body: e.data ? e.data.text() : ""}}; }
  const d = msg.data || (msg.notification ? msg.notification : msg);
  const title = d.title || "Onze kalender";
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || "",
    icon: "icon-192.png",
    badge: "icon-192.png",
    tag: d.tag || undefined,
    renotify: !!d.tag,
    data: {url: d.url || "./"}
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || "./", self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) { if (c.url.startsWith(self.registration.scope) && "focus" in c) { if ("navigate" in c && c.url !== target) c.navigate(target).catch(() => {}); return c.focus(); } }
    return self.clients.openWindow(target);
  }));
});
