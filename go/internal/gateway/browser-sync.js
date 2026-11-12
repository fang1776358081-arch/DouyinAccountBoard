/* Keep the existing draft and cursor checks. The socket only wakes that reader. */
(() => {
  'use strict';
  const sync = window.LiveSync;
  if (!sync || typeof WebSocket !== 'function') return;

  function withSocket(options) {
    let socket = null;
    let reconnect = null;
    let failures = 0;
    let started = false;
    let authExpired = false;
    let wakePending = false;
    let waking = false;

    function foreground() {
      return started && !authExpired && document.visibilityState !== 'hidden';
    }

    function cursor() {
      return typeof data !== 'undefined' ? data?.cursor : undefined;
    }

    function requestUpdate() {
      wakePending = true;
      if (waking || !started || document.visibilityState === 'hidden') return;
      if (options.blocked?.()) {
        // The original scheduler will defer the read and retry at its normal cadence.
        void controller.poke();
        return;
      }
      waking = true;
      void (async () => {
        try {
          while (wakePending && started && document.visibilityState !== 'hidden' && !options.blocked?.()) {
            wakePending = false;
            const wasReading = controller.getState().phase === 'syncing';
            await controller.poke();
            if (wasReading && started && !options.blocked?.()) await controller.poke();
          }
        } finally {
          waking = false;
        }
      })();
    }

    function retryConnection() {
      if (reconnect !== null || !foreground()) return;
      const delay = Math.min(30000, 1000 * Math.pow(2, Math.min(failures++, 5)));
      reconnect = setTimeout(() => {
        reconnect = null;
        connect();
      }, delay);
    }

    function connect() {
      if (!foreground() || socket || reconnect !== null || !options.ready?.() || !Number.isSafeInteger(cursor())) return;
      const address = new URL('/api/go/live', window.location.href);
      address.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      address.searchParams.set('cursor', String(cursor()));
      let connection;
      try {
        connection = new WebSocket(address);
      } catch {
        retryConnection();
        return;
      }
      socket = connection;
      connection.addEventListener('open', () => {
        if (socket !== connection) return;
        failures = 0;
        requestUpdate();
      });
      connection.addEventListener('message', event => {
        if (socket !== connection || !foreground()) return;
        try {
          const message = JSON.parse(event.data);
          if (message.changed || message.reload || message.changes?.length) requestUpdate();
        } catch {
          connection.close(1000);
        }
      });
      connection.addEventListener('close', event => {
        if (socket !== connection) return;
        socket = null;
        if (event.code === 1008) {
          authExpired = true;
          requestUpdate();
        } else {
          retryConnection();
          requestUpdate();
        }
      });
    }

    function disconnect() {
      if (reconnect !== null) clearTimeout(reconnect);
      reconnect = null;
      const connection = socket;
      socket = null;
      if (connection) connection.close(1000);
    }

    function visibility() {
      if (foreground()) {
        connect();
        if (wakePending) requestUpdate();
      } else {
        disconnect();
      }
    }

    function resume() {
      if (!foreground()) return;
      connect();
      requestUpdate();
    }

    const controller = sync.create({
      ...options,
      run: async request => {
        connect();
        const result = await options.run(request);
        if (socket?.readyState === WebSocket.OPEN && ['idle', 'updated'].includes(result?.status)) {
          return { ...result, nextDelayMs: 30000 };
        }
        return result;
      },
    });

    return Object.freeze({
      ...controller,
      start() {
        if (started) return controller.start();
        started = true;
        authExpired = false;
        document.addEventListener('visibilitychange', visibility);
        window.addEventListener('pagehide', disconnect);
        window.addEventListener('pageshow', resume);
        const firstRead = controller.start();
        connect();
        return firstRead;
      },
      stop() {
        started = false;
        document.removeEventListener('visibilitychange', visibility);
        window.removeEventListener('pagehide', disconnect);
        window.removeEventListener('pageshow', resume);
        disconnect();
        controller.stop();
      },
    });
  }

  window.LiveSync = Object.freeze({ ...sync, create: withSocket });
})();
