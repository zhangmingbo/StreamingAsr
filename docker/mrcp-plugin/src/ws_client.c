/*
 * ws_client.c
 *
 * 极简 WebSocket 客户端（RFC 6455）实现。
 * 依赖: POSIX socket / poll / pthread。
 */
#include "ws_client.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <netdb.h>

#define WS_MAX_PAYLOAD   (8 * 1024 * 1024)   /* 与 Gateway websockets max_size 一致 */
#define WS_CONNECT_TIMEOUT_MS 3000
#define WS_SEND_TIMEOUT_MS    200

struct ws_client_t {
	int fd;
	char host[128];
	int port;
	char path[256];
	pthread_mutex_t send_mutex;
	volatile int closed;
};

/* ── 工具函数 ─────────────────────────────────────────────── */

static void ws_rand_bytes(unsigned char *buf, int len)
{
	FILE *f = fopen("/dev/urandom", "rb");
	if (f) {
		size_t n = fread(buf, 1, (size_t)len, f);
		fclose(f);
		if (n == (size_t)len) {
			return;
		}
	}
	/* 回退: 时间 + 地址抖动（仅测试环境） */
	struct timeval tv;
	gettimeofday(&tv, NULL);
	unsigned int seed = (unsigned int)(tv.tv_sec ^ tv.tv_usec ^ (long)buf);
	srand(seed);
	for (int i = 0; i < len; i++) {
		buf[i] = (unsigned char)(rand() & 0xFF);
	}
}

static const char WS_B64_CHARS[] =
	"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static void ws_base64_encode(const unsigned char *in, int in_len, char *out)
{
	int i, j = 0;
	for (i = 0; i + 2 < in_len; i += 3) {
		out[j++] = WS_B64_CHARS[in[i] >> 2];
		out[j++] = WS_B64_CHARS[((in[i] & 0x03) << 4) | (in[i + 1] >> 4)];
		out[j++] = WS_B64_CHARS[((in[i + 1] & 0x0F) << 2) | (in[i + 2] >> 6)];
		out[j++] = WS_B64_CHARS[in[i + 2] & 0x3F];
	}
	if (i < in_len) {
		out[j++] = WS_B64_CHARS[in[i] >> 2];
		if (i + 1 < in_len) {
			out[j++] = WS_B64_CHARS[((in[i] & 0x03) << 4) | (in[i + 1] >> 4)];
			out[j++] = WS_B64_CHARS[(in[i + 1] & 0x0F) << 2];
		} else {
			out[j++] = WS_B64_CHARS[(in[i] & 0x03) << 4];
			out[j++] = '=';
		}
		out[j++] = '=';
	}
	out[j] = '\0';
}

static int ws_set_nonblock(int fd, int nonblock)
{
	int flags = fcntl(fd, F_GETFL, 0);
	if (flags < 0) {
		return -1;
	}
	if (nonblock) {
		flags |= O_NONBLOCK;
	} else {
		flags &= ~O_NONBLOCK;
	}
	return fcntl(fd, F_SETFL, flags);
}

/* 发送全部 len 字节；超时或失败返回 -1 */
static int ws_send_all(int fd, const unsigned char *data, int len, int timeout_ms)
{
	int sent = 0;
	struct timeval tv;
	tv.tv_sec = timeout_ms / 1000;
	tv.tv_usec = (timeout_ms % 1000) * 1000;
	setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
	while (sent < len) {
		int n = (int)send(fd, data + sent, (size_t)(len - sent), MSG_NOSIGNAL);
		if (n < 0) {
			if (errno == EINTR) {
				continue;
			}
			return -1;
		}
		if (n == 0) {
			return -1;
		}
		sent += n;
	}
	return 0;
}

/* 阻塞读满 len 字节；返回 0 成功，-1 失败 */
static int ws_recv_all(int fd, unsigned char *data, int len, int timeout_ms)
{
	int got = 0;
	while (got < len) {
		struct pollfd pfd;
		pfd.fd = fd;
		pfd.events = POLLIN;
		int r = poll(&pfd, 1, timeout_ms);
		if (r <= 0) {
			return -1;  /* 超时或错误 */
		}
		if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
			return -1;
		}
		int n = (int)recv(fd, data + got, (size_t)(len - got), 0);
		if (n <= 0) {
			return -1;
		}
		got += n;
	}
	return 0;
}

/* ── 连接与握手 ───────────────────────────────────────────── */

static int ws_tcp_connect(const char *host, int port, int timeout_ms)
{
	struct sockaddr_in addr;
	int fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0) {
		return -1;
	}

	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons((unsigned short)port);
	if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
		struct hostent *he = gethostbyname(host);
		if (!he) {
			close(fd);
			return -1;
		}
		memcpy(&addr.sin_addr, he->h_addr_list[0], (size_t)he->h_length);
	}

	ws_set_nonblock(fd, 1);
	int r = connect(fd, (struct sockaddr *)&addr, sizeof(addr));
	if (r < 0 && errno != EINPROGRESS) {
		close(fd);
		return -1;
	}
	struct pollfd pfd;
	pfd.fd = fd;
	pfd.events = POLLOUT;
	r = poll(&pfd, 1, timeout_ms);
	if (r <= 0 || (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))) {
		close(fd);
		return -1;
	}
	int err = 0;
	socklen_t err_len = sizeof(err);
	getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &err_len);
	if (err != 0) {
		close(fd);
		return -1;
	}
	ws_set_nonblock(fd, 0);

	int one = 1;
	setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
	return fd;
}

ws_client_t* ws_client_connect(const char *host, int port, const char *path,
                               char *err_buf, int err_len)
{
	ws_client_t *c = (ws_client_t *)calloc(1, sizeof(ws_client_t));
	if (!c) {
		snprintf(err_buf, (size_t)err_len, "calloc failed");
		return NULL;
	}
	c->fd = -1;
	c->closed = 0;
	snprintf(c->host, sizeof(c->host), "%s", host ? host : "127.0.0.1");
	c->port = port;
	snprintf(c->path, sizeof(c->path), "%s", path && path[0] ? path : "/");

	pthread_mutex_init(&c->send_mutex, NULL);

	c->fd = ws_tcp_connect(c->host, c->port, WS_CONNECT_TIMEOUT_MS);
	if (c->fd < 0) {
		snprintf(err_buf, (size_t)err_len, "tcp connect %s:%d failed: %s",
		         c->host, c->port, strerror(errno));
		ws_client_destroy(c);
		return NULL;
	}

	/* 构造 HTTP Upgrade 握手 */
	unsigned char key_bytes[16];
	char key_b64[32];
	ws_rand_bytes(key_bytes, 16);
	ws_base64_encode(key_bytes, 16, key_b64);

	char req[1024];
	int req_len = snprintf(req, sizeof(req),
		"GET %s HTTP/1.1\r\n"
		"Host: %s:%d\r\n"
		"Upgrade: websocket\r\n"
		"Connection: Upgrade\r\n"
		"Sec-WebSocket-Key: %s\r\n"
		"Sec-WebSocket-Version: 13\r\n"
		"\r\n",
		c->path, c->host, c->port, key_b64);

	if (ws_send_all(c->fd, (const unsigned char *)req, req_len, WS_SEND_TIMEOUT_MS) != 0) {
		snprintf(err_buf, (size_t)err_len, "send handshake failed: %s", strerror(errno));
		ws_client_destroy(c);
		return NULL;
	}

	/* 读响应直到 \r\n\r\n */
	char resp[4096];
	int resp_len = 0;
	int got_101 = 0;
	while (resp_len < (int)sizeof(resp) - 1) {
		struct pollfd pfd;
		pfd.fd = c->fd;
		pfd.events = POLLIN;
		int r = poll(&pfd, 1, WS_CONNECT_TIMEOUT_MS);
		if (r <= 0) {
			snprintf(err_buf, (size_t)err_len, "handshake response timeout");
			ws_client_destroy(c);
			return NULL;
		}
		int n = (int)recv(c->fd, resp + resp_len, sizeof(resp) - 1 - (size_t)resp_len, 0);
		if (n <= 0) {
			snprintf(err_buf, (size_t)err_len, "handshake response read failed");
			ws_client_destroy(c);
			return NULL;
		}
		resp_len += n;
		resp[resp_len] = '\0';
		char *end = strstr(resp, "\r\n\r\n");
		if (end) {
			/* 简单校验状态行 */
			if (strncmp(resp, "HTTP/1.1 101", 12) == 0 ||
			    strncmp(resp, "HTTP/1.0 101", 12) == 0) {
				got_101 = 1;
			} else {
				char *nl = strchr(resp, '\r');
				if (nl) {
					*nl = '\0';
				}
				snprintf(err_buf, (size_t)err_len, "handshake rejected: %.100s", resp);
				ws_client_destroy(c);
				return NULL;
			}
			break;
		}
	}
	if (!got_101) {
		snprintf(err_buf, (size_t)err_len, "handshake incomplete");
		ws_client_destroy(c);
		return NULL;
	}
	return c;
}

/* ── 发送帧 ───────────────────────────────────────────────── */

/* 发送一帧（客户端必须掩码）。内部调用需已持有 send_mutex 或由单一函数包装。 */
static int ws_frame_send(ws_client_t *c, int opcode, const void *data, int len)
{
	unsigned char mask[4];
	unsigned char header[14];
	int header_len;

	ws_rand_bytes(mask, 4);

	header[0] = (unsigned char)(0x80 | (opcode & 0x0F));  /* FIN=1 */
	if (len < 126) {
		header[1] = (unsigned char)(0x80 | len);          /* MASK=1 */
		header_len = 2;
	} else if (len < 65536) {
		header[1] = (unsigned char)(0x80 | 126);
		header[2] = (unsigned char)((len >> 8) & 0xFF);
		header[3] = (unsigned char)(len & 0xFF);
		header_len = 4;
	} else {
		header[1] = (unsigned char)(0x80 | 127);
		unsigned long long ll = (unsigned long long)len;
		for (int i = 0; i < 8; i++) {
			header[2 + i] = (unsigned char)((ll >> (56 - i * 8)) & 0xFF);
		}
		header_len = 10;
	}
	memcpy(header + header_len, mask, 4);

	/* 掩码 payload（复制，避免改调用方数据） */
	unsigned char *masked = (unsigned char *)malloc((size_t)(len > 0 ? len : 1));
	if (!masked) {
		return -1;
	}
	const unsigned char *src = (const unsigned char *)data;
	for (int i = 0; i < len; i++) {
		masked[i] = src[i] ^ mask[i % 4];
	}

	int ret = 0;
	if (ws_send_all(c->fd, header, header_len + 4, WS_SEND_TIMEOUT_MS) != 0) {
		ret = -1;
	} else if (len > 0 &&
	           ws_send_all(c->fd, masked, len, WS_SEND_TIMEOUT_MS) != 0) {
		ret = -1;
	}
	free(masked);
	return ret;
}

int ws_client_send_text(ws_client_t *c, const char *text, int len)
{
	if (!c || c->closed || c->fd < 0 || !text) {
		return -1;
	}
	if (len < 0) {
		len = (int)strlen(text);  /* 允许传 -1 表示 strlen */
	}
	pthread_mutex_lock(&c->send_mutex);
	int ret = ws_frame_send(c, 0x1, text, len);
	pthread_mutex_unlock(&c->send_mutex);
	return ret;
}

int ws_client_send_binary(ws_client_t *c, const void *data, int len)
{
	if (!c || c->closed || c->fd < 0 || !data || len < 0) {
		return -1;
	}
	pthread_mutex_lock(&c->send_mutex);
	int ret = ws_frame_send(c, 0x2, data, len);
	pthread_mutex_unlock(&c->send_mutex);
	return ret;
}

/* ── 接收帧 ───────────────────────────────────────────────── */

/* 读取帧头并返回 payload 长度（未掩码处理）。返回 <0 错误。 */
static long long ws_frame_read_header(int fd, int *opcode, int *fin,
                                      unsigned char *mask, int *masked)
{
	unsigned char hdr[2];
	if (ws_recv_all(fd, hdr, 2, 5000) != 0) {
		return -1;
	}
	*fin = (hdr[0] & 0x80) != 0;
	*opcode = hdr[0] & 0x0F;
	*masked = (hdr[1] & 0x80) != 0;
	long long len = hdr[1] & 0x7F;

	if (len == 126) {
		unsigned char ext[2];
		if (ws_recv_all(fd, ext, 2, 5000) != 0) {
			return -1;
		}
		len = ((long long)ext[0] << 8) | ext[1];
	} else if (len == 127) {
		unsigned char ext[8];
		if (ws_recv_all(fd, ext, 8, 5000) != 0) {
			return -1;
		}
		len = 0;
		for (int i = 0; i < 8; i++) {
			len = (len << 8) | ext[i];
		}
	}

	if (*masked) {
		if (ws_recv_all(fd, mask, 4, 5000) != 0) {
			return -1;
		}
	}
	if (len < 0 || len > WS_MAX_PAYLOAD) {
		return -1;
	}
	return len;
}

/* 读取并（可选）解码 payload 到 buf。 */
static int ws_frame_read_payload(int fd, unsigned char *buf, long long len,
                                 const unsigned char *mask, int masked)
{
	if (len == 0) {
		return 0;
	}
	if (ws_recv_all(fd, buf, (int)len, 10000) != 0) {
		return -1;
	}
	if (masked) {
		for (long long i = 0; i < len; i++) {
			buf[i] ^= mask[i % 4];
		}
	}
	return 0;
}

int ws_client_recv_text(ws_client_t *c, char *buf, int buf_len, int timeout_ms)
{
	if (!c || c->closed || c->fd < 0) {
		return -1;
	}

	/* 等待有数据可读 */
	struct pollfd pfd;
	pfd.fd = c->fd;
	pfd.events = POLLIN;
	int r = poll(&pfd, 1, timeout_ms);
	if (r <= 0) {
		return 0;  /* 超时 */
	}
	if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
		return -1;
	}

	int opcode, fin, masked;
	unsigned char mask[4];
	long long len = ws_frame_read_header(c->fd, &opcode, &fin, mask, &masked);
	if (len < 0) {
		return -1;
	}

	switch (opcode) {
	case 0x1: {  /* text */
		if (!fin) {
			/* 不支持分片文本: 丢弃该帧后续（简化） */
			return 0;
		}
		if (len >= buf_len) {
			/* buffer 不足: 丢弃 */
			unsigned char *tmp = (unsigned char *)malloc((size_t)len);
			if (tmp) {
				ws_frame_read_payload(c->fd, tmp, len, mask, masked);
				free(tmp);
			}
			return -1;
		}
		if (ws_frame_read_payload(c->fd, (unsigned char *)buf, len, mask, masked) != 0) {
			return -1;
		}
		buf[len] = '\0';
		return (int)len;
	}
	case 0x8: {  /* close */
		if (len > 0) {
			unsigned char tmp[128];
			ws_frame_read_payload(c->fd, tmp, len, mask, masked);
		}
		c->closed = 1;
		return -1;
	}
	case 0x9: {  /* ping -> pong */
		unsigned char *tmp = (unsigned char *)malloc((size_t)(len > 0 ? len : 1));
		if (!tmp) {
			return -1;
		}
		ws_frame_read_payload(c->fd, tmp, len, mask, masked);
		pthread_mutex_lock(&c->send_mutex);
		ws_frame_send(c, 0xA, tmp, (int)len);
		pthread_mutex_unlock(&c->send_mutex);
		free(tmp);
		return 0;
	}
	case 0xA: {  /* pong: 忽略 */
		unsigned char *tmp = (unsigned char *)malloc((size_t)(len > 0 ? len : 1));
		if (!tmp) {
			return -1;
		}
		ws_frame_read_payload(c->fd, tmp, len, mask, masked);
		free(tmp);
		return 0;
	}
	default: {  /* 0x2 binary 及其他: 丢弃 */
		unsigned char *tmp = (unsigned char *)malloc((size_t)(len > 0 ? len : 1));
		if (!tmp) {
			return -1;
		}
		ws_frame_read_payload(c->fd, tmp, len, mask, masked);
		free(tmp);
		return 0;
	}
	}
}

void ws_client_shutdown(ws_client_t *c)
{
	if (!c) {
		return;
	}
	/* 先发送 close 帧（尽力而为），避免对端报 "no close frame received" */
	if (c->fd >= 0 && !c->closed) {
		pthread_mutex_lock(&c->send_mutex);
		ws_frame_send(c, 0x8, NULL, 0);
		pthread_mutex_unlock(&c->send_mutex);
	}
	if (c->fd >= 0) {
		shutdown(c->fd, SHUT_RDWR);
	}
	c->closed = 1;
}

void ws_client_destroy(ws_client_t *c)
{
	if (!c) {
		return;
	}
	if (c->fd >= 0) {
		close(c->fd);
		c->fd = -1;
	}
	pthread_mutex_destroy(&c->send_mutex);
	free(c);
}
