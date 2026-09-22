/*
 * ws_client.h
 *
 * 极简 WebSocket 客户端（RFC 6455）— 用于 mrcp-funasr 插件连接
 * Gateway 内部 WS 通道（:5003）。
 *
 * 特性:
 *   - 客户端帧按 RFC 6455 要求掩码
 *   - 自动回复 ping
 *   - 发送超时 200ms（保证 MPF stream 回调不阻塞）
 *   - 文本帧接收带超时（供接收线程轮询）
 *
 * 线程模型: 发送线程安全（内部互斥锁）；接收仅由单一接收线程调用。
 */
#ifndef WS_CLIENT_H
#define WS_CLIENT_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ws_client_t ws_client_t;

/** 建立 WS 连接（含 HTTP Upgrade 握手）。失败返回 NULL 并填充 err_buf。 */
ws_client_t* ws_client_connect(const char *host, int port, const char *path,
                               char *err_buf, int err_len);

/** 发送文本帧。返回 0 成功，-1 失败。 */
int ws_client_send_text(ws_client_t *c, const char *text, int len);

/** 发送二进制帧。返回 0 成功，-1 失败。 */
int ws_client_send_binary(ws_client_t *c, const void *data, int len);

/**
 * 阻塞接收一帧文本（超时 timeout_ms 毫秒）。
 * 返回 >0: payload 长度（buf 已填，'\0' 结尾）;
 * 返回 0: 连接关闭或超时（以 errno/状态区分）;
 * 返回 -1: 错误。
 * 非文本帧（ping/binary）在内部处理或跳过。
 */
int ws_client_recv_text(ws_client_t *c, char *buf, int buf_len, int timeout_ms);

/** 关闭底层 socket（唤醒阻塞的 recv），随后可 ws_client_destroy。 */
void ws_client_shutdown(ws_client_t *c);

/** 销毁客户端并释放资源。 */
void ws_client_destroy(ws_client_t *c);

#ifdef __cplusplus
}
#endif

#endif /* WS_CLIENT_H */
