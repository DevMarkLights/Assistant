// Service worker for push notifications only. There is deliberately no fetch handler: nothing is
// cached, so the installed app always loads the current frontend from the server.
// Served at /Assistant/sw.js, so its scope is /Assistant/.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data;
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data && event.data.text() };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Assistant", {
      body: data.body || "",
      icon: "icon-192.png",
      badge: "icon-192.png",
      data: { url: data.url || "./" },
    })
  );
});

// Tapping a notification focuses an open app window (navigating it to the URL) or opens a new one
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data.url, self.registration.scope).href;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        if (client.url.startsWith(self.registration.scope) && "focus" in client) {
          await client.focus();
          return client.navigate(url);
        }
      }
      return self.clients.openWindow(url);
    })()
  );
});
