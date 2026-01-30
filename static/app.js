// Svengalibot frontend utilities

const Svengali = {
    // Format a diff with syntax highlighting
    formatDiff(diff) {
        return diff.split('\n').map(line => {
            if (line.startsWith('+') && !line.startsWith('+++')) {
                return `<span class="diff-add">${this.escapeHtml(line)}</span>`;
            } else if (line.startsWith('-') && !line.startsWith('---')) {
                return `<span class="diff-remove">${this.escapeHtml(line)}</span>`;
            } else if (line.startsWith('@@')) {
                return `<span class="diff-hunk">${this.escapeHtml(line)}</span>`;
            }
            return this.escapeHtml(line);
        }).join('\n');
    },

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    // Create an SSE connection with auto-reconnect
    createEventSource(url, handlers) {
        let evtSource = new EventSource(url);
        let reconnectAttempts = 0;
        const maxReconnect = 10;

        evtSource.onmessage = (event) => {
            reconnectAttempts = 0;
            const data = JSON.parse(event.data);
            if (handlers.onMessage) handlers.onMessage(data);
        };

        evtSource.onerror = () => {
            evtSource.close();
            if (reconnectAttempts < maxReconnect) {
                reconnectAttempts++;
                setTimeout(() => {
                    if (handlers.onReconnect) handlers.onReconnect(reconnectAttempts);
                    this.createEventSource(url, handlers);
                }, Math.min(1000 * reconnectAttempts, 10000));
            } else {
                if (handlers.onMaxReconnect) handlers.onMaxReconnect();
            }
        };

        return evtSource;
    },

    // Poll an endpoint at intervals
    poll(url, callback, interval = 2000) {
        const doPoll = async () => {
            try {
                const response = await fetch(url);
                const data = await response.json();
                callback(data);
            } catch (e) {
                console.error('Poll error:', e);
            }
        };

        doPoll();
        return setInterval(doPoll, interval);
    },

    // API helpers
    async post(url, data = {}) {
        const response = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        return response.json();
    },

    async put(url, data = {}) {
        const response = await fetch(url, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        return response.json();
    }
};
