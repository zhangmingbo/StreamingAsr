/*
 * mrcp_funasr_engine.c
 *
 * FunASR 流式语音识别引擎 — UniMRCP 插件（Argos ASR v5.7）
 *
 * 基于 UniMRCP demo-recog 插件改造：
 *   - Task 3: 插件骨架（engine/channel/stream vtable + 消息泵 + RECOGNIZE 全流程）
 *   - Task 4: 音频转发（PCM 帧 -> Gateway 内部 WS 通道 :5003 二进制帧）
 *   - Task 5: 事件映射（WS 下行事件 -> START-OF-INPUT / RECOGNITION-COMPLETE + NLSML）
 *
 * Mandatory rules concerning plugin implementation.
 * 1. Each plugin MUST implement a plugin/engine creator function
 *    with the exact signature and name (the main entry point)
 *        MRCP_PLUGIN_DECLARE(mrcp_engine_t*) mrcp_plugin_create(apr_pool_t *pool)
 * 2. Each plugin MUST declare its version number
 *        MRCP_PLUGIN_VERSION_DECLARE
 * 3. One and only one response MUST be sent back to the received request.
 * 4. Methods (callbacks) of the MRCP engine channel MUST not block.
 *    (asynchronous response can be sent from the context of other thread)
 * 5. Methods (callbacks) of the MPF engine stream MUST not block.
 */

#include "mrcp_recog_engine.h"
#include "mrcp_engine_impl.h"
#include "mpf_activity_detector.h"
#include "apt_consumer_task.h"
#include "apt_log.h"
#include "ws_client.h"

#include <apr_thread_proc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FUNASR_ENGINE_TASK_NAME "FunASR Engine"
#define FUNASR_WS_RECV_BUF_SIZE 65536

typedef struct mrcp_funasr_engine_t mrcp_funasr_engine_t;
typedef struct mrcp_funasr_channel_t mrcp_funasr_channel_t;
typedef struct mrcp_funasr_msg_t mrcp_funasr_msg_t;

/** Declaration of recognizer engine methods */
static apt_bool_t mrcp_funasr_engine_destroy(mrcp_engine_t *engine);
static apt_bool_t mrcp_funasr_engine_open(mrcp_engine_t *engine);
static apt_bool_t mrcp_funasr_engine_close(mrcp_engine_t *engine);
static mrcp_engine_channel_t* mrcp_funasr_engine_channel_create(mrcp_engine_t *engine, apr_pool_t *pool);

static const struct mrcp_engine_method_vtable_t engine_vtable = {
	mrcp_funasr_engine_destroy,
	mrcp_funasr_engine_open,
	mrcp_funasr_engine_close,
	mrcp_funasr_engine_channel_create
};

/** Declaration of recognizer channel methods */
static apt_bool_t mrcp_funasr_channel_destroy(mrcp_engine_channel_t *channel);
static apt_bool_t mrcp_funasr_channel_open(mrcp_engine_channel_t *channel);
static apt_bool_t mrcp_funasr_channel_close(mrcp_engine_channel_t *channel);
static apt_bool_t mrcp_funasr_channel_request_process(mrcp_engine_channel_t *channel, mrcp_message_t *request);

static const struct mrcp_engine_channel_method_vtable_t channel_vtable = {
	mrcp_funasr_channel_destroy,
	mrcp_funasr_channel_open,
	mrcp_funasr_channel_close,
	mrcp_funasr_channel_request_process
};

/** Declaration of recognizer audio stream methods */
static apt_bool_t mrcp_funasr_stream_destroy(mpf_audio_stream_t *stream);
static apt_bool_t mrcp_funasr_stream_open(mpf_audio_stream_t *stream, mpf_codec_t *codec);
static apt_bool_t mrcp_funasr_stream_close(mpf_audio_stream_t *stream);
static apt_bool_t mrcp_funasr_stream_write(mpf_audio_stream_t *stream, const mpf_frame_t *frame);

static const mpf_audio_stream_vtable_t audio_stream_vtable = {
	mrcp_funasr_stream_destroy,
	NULL,
	NULL,
	NULL,
	mrcp_funasr_stream_open,
	mrcp_funasr_stream_close,
	mrcp_funasr_stream_write,
	NULL
};

/** Declaration of FunASR recognizer engine */
struct mrcp_funasr_engine_t {
	apt_consumer_task_t    *task;
};

/** Declaration of FunASR recognizer channel */
struct mrcp_funasr_channel_t {
	/** Back pointer to engine */
	mrcp_funasr_engine_t     *funasr_engine;
	/** Engine channel base */
	mrcp_engine_channel_t   *channel;

	/** Active (in-progress) recognition request */
	mrcp_message_t          *recog_request;
	/** Pending stop response */
	mrcp_message_t          *stop_response;
	/** Indicates whether input timers are started */
	apt_bool_t               timers_started;
	/** Voice activity detector */
	mpf_activity_detector_t *detector;
	/** File to write utterance to (调试用，可关闭) */
	FILE                    *audio_out;

	/** Gateway 内部 WS 通道（Task 4） */
	ws_client_t             *gateway_ws;
	apr_thread_t            *ws_thread;
	volatile int             ws_stop;
	const char              *gateway_host;
	int                      gateway_port;
	const char              *gateway_path;

	/** Task 5: 事件映射状态（WS 线程与 MPF/task 线程共享，mutex 保护） */
	apr_thread_mutex_t      *mutex;
	char                     result_text[4096];
	int                      result_len;
	apt_bool_t               start_of_input_sent;
	apt_bool_t               recognition_sent;
	/** no-input 超时（毫秒，0=未设置），传递给 Gateway 定时器 */
	int                      no_input_timeout;
};

typedef enum {
	FUNASR_MSG_OPEN_CHANNEL,
	FUNASR_MSG_CLOSE_CHANNEL,
	FUNASR_MSG_REQUEST_PROCESS
} mrcp_funasr_msg_type_e;

/** Declaration of FunASR task message */
struct mrcp_funasr_msg_t {
	mrcp_funasr_msg_type_e  type;
	mrcp_engine_channel_t *channel;
	mrcp_message_t        *request;
};

static apt_bool_t mrcp_funasr_msg_signal(mrcp_funasr_msg_type_e type, mrcp_engine_channel_t *channel, mrcp_message_t *request);
static apt_bool_t mrcp_funasr_msg_process(apt_task_t *task, apt_task_msg_t *msg);

/** Gateway WS 接收线程（Task 4 音频转发 / Task 5 事件映射） */
static void* APR_THREAD_FUNC mrcp_funasr_ws_thread_main(apr_thread_t *thread, void *obj);

/** 极简 JSON 字符串字段提取: 提取 {"key": "value"} 中的 value */
static apt_bool_t json_extract_string(const char *json, const char *key, char *out, int out_len);

/** Declare this macro to set plugin version */
MRCP_PLUGIN_VERSION_DECLARE

/**
 * Declare this macro to use log routine of the server, plugin is loaded from.
 * Enable/add the corresponding entry in logger.xml to set a cutsom log source priority.
 *    <source name="FUNASR-PLUGIN" priority="DEBUG" masking="NONE"/>
 */
MRCP_PLUGIN_LOG_SOURCE_IMPLEMENT(FUNASR_PLUGIN,"FUNASR-PLUGIN")

/** Use custom log source mark */
#define FUNASR_LOG_MARK   APT_LOG_MARK_DECLARE(FUNASR_PLUGIN)

/** Create FunASR recognizer engine */
MRCP_PLUGIN_DECLARE(mrcp_engine_t*) mrcp_plugin_create(apr_pool_t *pool)
{
	mrcp_funasr_engine_t *funasr_engine = apr_palloc(pool,sizeof(mrcp_funasr_engine_t));
	apt_task_t *task;
	apt_task_vtable_t *vtable;
	apt_task_msg_pool_t *msg_pool;

	msg_pool = apt_task_msg_pool_create_dynamic(sizeof(mrcp_funasr_msg_t),pool);
	funasr_engine->task = apt_consumer_task_create(funasr_engine,msg_pool,pool);
	if(!funasr_engine->task) {
		return NULL;
	}
	task = apt_consumer_task_base_get(funasr_engine->task);
	apt_task_name_set(task,FUNASR_ENGINE_TASK_NAME);
	vtable = apt_task_vtable_get(task);
	if(vtable) {
		vtable->process_msg = mrcp_funasr_msg_process;
	}

	/* create engine base */
	return mrcp_engine_create(
				MRCP_RECOGNIZER_RESOURCE,  /* MRCP resource identifier */
				funasr_engine,             /* object to associate */
				&engine_vtable,            /* virtual methods table of engine */
				pool);                     /* pool to allocate memory from */
}

/** Destroy recognizer engine */
static apt_bool_t mrcp_funasr_engine_destroy(mrcp_engine_t *engine)
{
	mrcp_funasr_engine_t *funasr_engine = engine->obj;
	if(funasr_engine->task) {
		apt_task_t *task = apt_consumer_task_base_get(funasr_engine->task);
		apt_task_destroy(task);
		funasr_engine->task = NULL;
	}
	return TRUE;
}

/** Open recognizer engine */
static apt_bool_t mrcp_funasr_engine_open(mrcp_engine_t *engine)
{
	mrcp_funasr_engine_t *funasr_engine = engine->obj;
	if(funasr_engine->task) {
		apt_task_t *task = apt_consumer_task_base_get(funasr_engine->task);
		apt_task_start(task);
	}
	return mrcp_engine_open_respond(engine,TRUE);
}

/** Close recognizer engine */
static apt_bool_t mrcp_funasr_engine_close(mrcp_engine_t *engine)
{
	mrcp_funasr_engine_t *funasr_engine = engine->obj;
	if(funasr_engine->task) {
		apt_task_t *task = apt_consumer_task_base_get(funasr_engine->task);
		apt_task_terminate(task,TRUE);
	}
	return mrcp_engine_close_respond(engine);
}

static mrcp_engine_channel_t* mrcp_funasr_engine_channel_create(mrcp_engine_t *engine, apr_pool_t *pool)
{
	mpf_stream_capabilities_t *capabilities;
	mpf_termination_t *termination;

	/* create FunASR channel */
	mrcp_funasr_channel_t *funasr_channel = apr_palloc(pool,sizeof(mrcp_funasr_channel_t));
	funasr_channel->funasr_engine = engine->obj;
	funasr_channel->recog_request = NULL;
	funasr_channel->stop_response = NULL;
	funasr_channel->detector = mpf_activity_detector_create(pool);
	funasr_channel->audio_out = NULL;
	funasr_channel->gateway_ws = NULL;
	funasr_channel->ws_thread = NULL;
	funasr_channel->ws_stop = 0;
	funasr_channel->mutex = NULL;
	if(apr_thread_mutex_create(&funasr_channel->mutex,APR_THREAD_MUTEX_DEFAULT,pool) != APR_SUCCESS) {
		funasr_channel->mutex = NULL;
	}
	funasr_channel->result_len = 0;
	funasr_channel->result_text[0] = '\0';
	funasr_channel->start_of_input_sent = FALSE;
	funasr_channel->recognition_sent = FALSE;

	/* Gateway 地址（unimrcpserver.xml 引擎 <param> 可覆盖，见 Task 4 部署说明） */
	const char *gwhost = mrcp_engine_param_get(engine, "gateway-host");
	const char *gwport = mrcp_engine_param_get(engine, "gateway-port");
	const char *gwpath = mrcp_engine_param_get(engine, "gateway-path");
	funasr_channel->gateway_host = gwhost ? gwhost : "172.17.0.3";
	funasr_channel->gateway_port = gwport ? atoi(gwport) : 5003;
	funasr_channel->gateway_path = (gwpath && gwpath[0]) ? gwpath : "/";

	capabilities = mpf_sink_stream_capabilities_create(pool);
	mpf_codec_capabilities_add(
			&capabilities->codecs,
			MPF_SAMPLE_RATE_8000 | MPF_SAMPLE_RATE_16000,
			"LPCM");

	/* create media termination */
	termination = mrcp_engine_audio_termination_create(
			funasr_channel,        /* object to associate */
			&audio_stream_vtable,  /* virtual methods table of audio stream */
			capabilities,          /* stream capabilities */
			pool);                 /* pool to allocate memory from */

	/* create engine channel base */
	funasr_channel->channel = mrcp_engine_channel_create(
			engine,               /* engine */
			&channel_vtable,      /* virtual methods table of engine channel */
			funasr_channel,       /* object to associate */
			termination,          /* associated media termination */
			pool);                /* pool to allocate memory from */

	return funasr_channel->channel;
}

/** Destroy engine channel */
static apt_bool_t mrcp_funasr_channel_destroy(mrcp_engine_channel_t *channel)
{
	mrcp_funasr_channel_t *funasr_channel = channel->method_obj;
	if(funasr_channel->mutex) {
		apr_thread_mutex_destroy(funasr_channel->mutex);
		funasr_channel->mutex = NULL;
	}
	return TRUE;
}

/** Open engine channel (asynchronous response MUST be sent)*/
static apt_bool_t mrcp_funasr_channel_open(mrcp_engine_channel_t *channel)
{
	if(channel->attribs) {
		/* process attributes */
		const apr_array_header_t *header = apr_table_elts(channel->attribs);
		apr_table_entry_t *entry = (apr_table_entry_t *)header->elts;
		int i;
		for(i=0; i<header->nelts; i++) {
			apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Attrib name [%s] value [%s]",entry[i].key,entry[i].val);
		}
	}

	return mrcp_funasr_msg_signal(FUNASR_MSG_OPEN_CHANNEL,channel,NULL);
}

/** Close engine channel (asynchronous response MUST be sent)*/
static apt_bool_t mrcp_funasr_channel_close(mrcp_engine_channel_t *channel)
{
	return mrcp_funasr_msg_signal(FUNASR_MSG_CLOSE_CHANNEL,channel,NULL);
}

/** Process MRCP channel request (asynchronous response MUST be sent)*/
static apt_bool_t mrcp_funasr_channel_request_process(mrcp_engine_channel_t *channel, mrcp_message_t *request)
{
	return mrcp_funasr_msg_signal(FUNASR_MSG_REQUEST_PROCESS,channel,request);
}

/** Process RECOGNIZE request */
static apt_bool_t mrcp_funasr_channel_recognize(mrcp_engine_channel_t *channel, mrcp_message_t *request, mrcp_message_t *response)
{
	/* process RECOGNIZE request */
	mrcp_recog_header_t *recog_header;
	mrcp_funasr_channel_t *funasr_channel = channel->method_obj;
	const mpf_codec_descriptor_t *descriptor = mrcp_engine_sink_stream_codec_get(channel);

	if(!descriptor) {
		apt_log(FUNASR_LOG_MARK,APT_PRIO_WARNING,"Failed to Get Codec Descriptor " APT_SIDRES_FMT, MRCP_MESSAGE_SIDRES(request));
		response->start_line.status_code = MRCP_STATUS_CODE_METHOD_FAILED;
		return FALSE;
	}

	funasr_channel->timers_started = TRUE;

	/* get recognizer header */
	recog_header = mrcp_resource_header_get(request);
	if(recog_header) {
		if(mrcp_resource_header_property_check(request,RECOGNIZER_HEADER_START_INPUT_TIMERS) == TRUE) {
			funasr_channel->timers_started = recog_header->start_input_timers;
		}
		if(mrcp_resource_header_property_check(request,RECOGNIZER_HEADER_NO_INPUT_TIMEOUT) == TRUE) {
			funasr_channel->no_input_timeout = recog_header->no_input_timeout;
			mpf_activity_detector_noinput_timeout_set(funasr_channel->detector,recog_header->no_input_timeout);
		}
		if(mrcp_resource_header_property_check(request,RECOGNIZER_HEADER_SPEECH_COMPLETE_TIMEOUT) == TRUE) {
			mpf_activity_detector_silence_timeout_set(funasr_channel->detector,recog_header->speech_complete_timeout);
		}
	}

	/* Task 4: 向 Gateway 内部 WS 通道发起识别开始（一个 WS 连接 = 一个识别会话） */
	if(funasr_channel->gateway_ws) {
		char start_msg[128];
		int n = snprintf(start_msg,sizeof(start_msg),
			"{\"type\":\"start\",\"sample_rate\":%d,\"no_input_timeout\":%d}",
			descriptor->sampling_rate, (int)funasr_channel->no_input_timeout);
		if(ws_client_send_text(funasr_channel->gateway_ws,start_msg,n) != 0) {
			apt_log(FUNASR_LOG_MARK,APT_PRIO_WARNING,"Failed to Send start to Gateway WS " APT_SIDRES_FMT,
				MRCP_MESSAGE_SIDRES(request));
		}
	}

	if(!funasr_channel->audio_out) {
		const apt_dir_layout_t *dir_layout = channel->engine->dir_layout;
		char *file_name = apr_psprintf(channel->pool,"utter-%dkHz-%s.pcm",
							descriptor->sampling_rate/1000,
							request->channel_id.session_id.buf);
		char *file_path = apt_vardir_filepath_get(dir_layout,file_name,channel->pool);
		if(file_path) {
			apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Open Utterance Output File [%s] for Writing",file_path);
			funasr_channel->audio_out = fopen(file_path,"wb");
			if(!funasr_channel->audio_out) {
				apt_log(FUNASR_LOG_MARK,APT_PRIO_WARNING,"Failed to Open Utterance Output File [%s] for Writing",file_path);
			}
		}
	}

	response->start_line.request_state = MRCP_REQUEST_STATE_INPROGRESS;
	/* send asynchronous response */
	mrcp_engine_channel_message_send(channel,response);

	/* Task 5: 重置事件映射状态 */
	if(funasr_channel->mutex) {
		apr_thread_mutex_lock(funasr_channel->mutex);
	}
	funasr_channel->start_of_input_sent = FALSE;
	funasr_channel->recognition_sent = FALSE;
	funasr_channel->result_len = 0;
	funasr_channel->result_text[0] = '\0';
	if(funasr_channel->mutex) {
		apr_thread_mutex_unlock(funasr_channel->mutex);
	}

	funasr_channel->recog_request = request;
	return TRUE;
}

/** Process STOP request */
static apt_bool_t mrcp_funasr_channel_stop(mrcp_engine_channel_t *channel, mrcp_message_t *request, mrcp_message_t *response)
{
	/* process STOP request */
	mrcp_funasr_channel_t *funasr_channel = channel->method_obj;

	/* Task 4: 通知 Gateway 结束识别 */
	if(funasr_channel->gateway_ws) {
		ws_client_send_text(funasr_channel->gateway_ws,"{\"type\":\"end\"}",-1);
	}

	/* 立即响应 STOP（不再等待后续音频帧） */
	mrcp_engine_channel_message_send(channel,response);
	if(funasr_channel->mutex) {
		apr_thread_mutex_lock(funasr_channel->mutex);
	}
	funasr_channel->recog_request = NULL;
	if(funasr_channel->mutex) {
		apr_thread_mutex_unlock(funasr_channel->mutex);
	}
	return TRUE;
}

/** Process START-INPUT-TIMERS request */
static apt_bool_t mrcp_funasr_channel_timers_start(mrcp_engine_channel_t *channel, mrcp_message_t *request, mrcp_message_t *response)
{
	mrcp_funasr_channel_t *funasr_channel = channel->method_obj;
	funasr_channel->timers_started = TRUE;
	return mrcp_engine_channel_message_send(channel,response);
}

/** Dispatch MRCP request */
static apt_bool_t mrcp_funasr_channel_request_dispatch(mrcp_engine_channel_t *channel, mrcp_message_t *request)
{
	apt_bool_t processed = FALSE;
	mrcp_message_t *response = mrcp_response_create(request,request->pool);
	switch(request->start_line.method_id) {
		case RECOGNIZER_SET_PARAMS:
			break;
		case RECOGNIZER_GET_PARAMS:
			break;
		case RECOGNIZER_DEFINE_GRAMMAR:
			break;
		case RECOGNIZER_RECOGNIZE:
			processed = mrcp_funasr_channel_recognize(channel,request,response);
			break;
		case RECOGNIZER_GET_RESULT:
			break;
		case RECOGNIZER_START_INPUT_TIMERS:
			processed = mrcp_funasr_channel_timers_start(channel,request,response);
			break;
		case RECOGNIZER_STOP:
			processed = mrcp_funasr_channel_stop(channel,request,response);
			break;
		default:
			break;
	}
	if(processed == FALSE) {
		/* send asynchronous response for not handled request */
		mrcp_engine_channel_message_send(channel,response);
	}
	return TRUE;
}

/** Callback is called from MPF engine context to destroy any additional data associated with audio stream */
static apt_bool_t mrcp_funasr_stream_destroy(mpf_audio_stream_t *stream)
{
	return TRUE;
}

/** Callback is called from MPF engine context to perform any action before open */
static apt_bool_t mrcp_funasr_stream_open(mpf_audio_stream_t *stream, mpf_codec_t *codec)
{
	return TRUE;
}

/** Callback is called from MPF engine context to perform any action after close */
static apt_bool_t mrcp_funasr_stream_close(mpf_audio_stream_t *stream)
{
	return TRUE;
}

/* Raise FunASR START-OF-INPUT event（Task 5: 去重 + 线程安全） */
static apt_bool_t mrcp_funasr_start_of_input(mrcp_funasr_channel_t *funasr_channel)
{
	mrcp_message_t *message;

	if(funasr_channel->mutex) {
		apr_thread_mutex_lock(funasr_channel->mutex);
	}
	if(funasr_channel->start_of_input_sent == TRUE || !funasr_channel->recog_request) {
		if(funasr_channel->mutex) {
			apr_thread_mutex_unlock(funasr_channel->mutex);
		}
		return FALSE;
	}
	funasr_channel->start_of_input_sent = TRUE;

	/* create START-OF-INPUT event */
	message = mrcp_event_create(
						funasr_channel->recog_request,
						RECOGNIZER_START_OF_INPUT,
						funasr_channel->recog_request->pool);
	if(!message) {
		if(funasr_channel->mutex) {
			apr_thread_mutex_unlock(funasr_channel->mutex);
		}
		return FALSE;
	}

	/* set request state */
	message->start_line.request_state = MRCP_REQUEST_STATE_INPROGRESS;
	/* send asynch event */
	mrcp_engine_channel_message_send(funasr_channel->channel,message);

	if(funasr_channel->mutex) {
		apr_thread_mutex_unlock(funasr_channel->mutex);
	}
	return TRUE;
}

/* XML 转义（NLSML instance/input 内容安全） */
static void mrcp_funasr_xml_escape(const char *src, char *dst, int dst_len)
{
	int i = 0;
	const char *p = src;
	while(*p && i < dst_len - 8) {
		switch(*p) {
			case '&':
				memcpy(dst + i,"&amp;",5);
				i += 5;
				break;
			case '<':
				memcpy(dst + i,"&lt;",4);
				i += 4;
				break;
			case '>':
				memcpy(dst + i,"&gt;",4);
				i += 4;
				break;
			case '"':
				memcpy(dst + i,"&quot;",6);
				i += 6;
				break;
			case '\'':
				memcpy(dst + i,"&apos;",6);
				i += 6;
				break;
			default:
				dst[i++] = *p;
				break;
		}
		p++;
	}
	dst[i] = '\0';
}

/* Load FunASR recognition result (Task 5: 基于 WS 下行事件累积的文本动态构建 NLSML) */
static apt_bool_t mrcp_funasr_result_load(mrcp_funasr_channel_t *funasr_channel, mrcp_message_t *message)
{
	mrcp_generic_header_t *generic_header;
	char escaped[8192];
	const char *text;
	const char *body;

	text = funasr_channel->result_text[0] ? funasr_channel->result_text : "";
	mrcp_funasr_xml_escape(text,escaped,sizeof(escaped));

	/* NLSML 结果（MRCPv2 标准格式，<instance> 为 FreeSWITCH 提取的识别文本） */
	body = apr_psprintf(message->pool,
		"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
		"<result xmlns=\"http://www.ietf.org/xml/ns/mrcpv2\">\n"
		"  <interpretation grammar=\"session:builtin\" confidence=\"0.95\">\n"
		"    <instance>%s</instance>\n"
		"    <input mode=\"speech\">%s</input>\n"
		"  </interpretation>\n"
		"</result>\n",
		escaped,escaped);
	apt_string_assign(&message->body,body,message->pool);

	/* get/allocate generic header */
	generic_header = mrcp_generic_header_prepare(message);
	if(generic_header) {
		/* set content types */
		apt_string_assign(&generic_header->content_type,"application/x-nlsml",message->pool);
		mrcp_generic_header_property_add(message,GENERIC_HEADER_CONTENT_TYPE);
	}
	return TRUE;
}

/* Raise FunASR RECOGNITION-COMPLETE event（Task 5: 防重 + 线程安全，WS 线程/task 线程均可调用） */
static apt_bool_t mrcp_funasr_recognition_complete(mrcp_funasr_channel_t *funasr_channel, mrcp_recog_completion_cause_e cause)
{
	mrcp_recog_header_t *recog_header;
	mrcp_message_t *message;

	if(funasr_channel->mutex) {
		apr_thread_mutex_lock(funasr_channel->mutex);
	}
	if(funasr_channel->recognition_sent == TRUE || !funasr_channel->recog_request) {
		if(funasr_channel->mutex) {
			apr_thread_mutex_unlock(funasr_channel->mutex);
		}
		return FALSE;
	}
	funasr_channel->recognition_sent = TRUE;

	/* create RECOGNITION-COMPLETE event */
	message = mrcp_event_create(
						funasr_channel->recog_request,
						RECOGNIZER_RECOGNITION_COMPLETE,
						funasr_channel->recog_request->pool);
	if(!message) {
		if(funasr_channel->mutex) {
			apr_thread_mutex_unlock(funasr_channel->mutex);
		}
		return FALSE;
	}

	/* get/allocate recognizer header */
	recog_header = mrcp_resource_header_prepare(message);
	if(recog_header) {
		/* set completion cause */
		recog_header->completion_cause = cause;
		mrcp_resource_header_property_add(message,RECOGNIZER_HEADER_COMPLETION_CAUSE);
	}
	/* set request state */
	message->start_line.request_state = MRCP_REQUEST_STATE_COMPLETE;

	if(cause == RECOGNIZER_COMPLETION_CAUSE_SUCCESS) {
		mrcp_funasr_result_load(funasr_channel,message);
	}

	funasr_channel->recog_request = NULL;
	/* send asynch event */
	mrcp_engine_channel_message_send(funasr_channel->channel,message);

	if(funasr_channel->mutex) {
		apr_thread_mutex_unlock(funasr_channel->mutex);
	}
	return TRUE;
}

/** Callback is called from MPF engine context to write/send new frame */
static apt_bool_t mrcp_funasr_stream_write(mpf_audio_stream_t *stream, const mpf_frame_t *frame)
{
	mrcp_funasr_channel_t *funasr_channel = stream->obj;
	if(funasr_channel->stop_response) {
		/* send asynchronous response to STOP request */
		mrcp_engine_channel_message_send(funasr_channel->channel,funasr_channel->stop_response);
		funasr_channel->stop_response = NULL;
		funasr_channel->recog_request = NULL;
		return TRUE;
	}

	if(funasr_channel->recog_request) {
		/* Task 4: 将 PCM 转发到 Gateway WS 通道（二进制帧，协商采样率 LPCM） */
		if(funasr_channel->gateway_ws && frame->codec_frame.size > 0) {
			ws_client_send_binary(funasr_channel->gateway_ws,
				frame->codec_frame.buffer,(int)frame->codec_frame.size);
		}

		mpf_detector_event_e det_event = mpf_activity_detector_process(funasr_channel->detector,frame);
		switch(det_event) {
			case MPF_DETECTOR_EVENT_ACTIVITY:
				apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Detected Voice Activity " APT_SIDRES_FMT,
					MRCP_MESSAGE_SIDRES(funasr_channel->recog_request));
				mrcp_funasr_start_of_input(funasr_channel);
				break;
			case MPF_DETECTOR_EVENT_INACTIVITY:
				/* Task 5: 识别完成由 Gateway speech_end/finished 事件驱动；
				   本地 VAD 仅用于日志与 START-OF-INPUT */
				apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Detected Voice Inactivity " APT_SIDRES_FMT,
					MRCP_MESSAGE_SIDRES(funasr_channel->recog_request));
				break;
			case MPF_DETECTOR_EVENT_NOINPUT:
				/* Task 5: no-input 由 Gateway finished(空文本) 事件驱动 */
				apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Detected Noinput " APT_SIDRES_FMT,
					MRCP_MESSAGE_SIDRES(funasr_channel->recog_request));
				break;
			default:
				break;
		}

		if(funasr_channel->recog_request) {
			if((frame->type & MEDIA_FRAME_TYPE_EVENT) == MEDIA_FRAME_TYPE_EVENT) {
				if(frame->marker == MPF_MARKER_START_OF_EVENT) {
					apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Detected Start of Event " APT_SIDRES_FMT " id:%d",
						MRCP_MESSAGE_SIDRES(funasr_channel->recog_request),
						frame->event_frame.event_id);
				}
				else if(frame->marker == MPF_MARKER_END_OF_EVENT) {
					apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Detected End of Event " APT_SIDRES_FMT " id:%d duration:%d ts",
						MRCP_MESSAGE_SIDRES(funasr_channel->recog_request),
						frame->event_frame.event_id,
						frame->event_frame.duration);
				}
			}
		}

		if(funasr_channel->audio_out) {
			fwrite(frame->codec_frame.buffer,1,frame->codec_frame.size,funasr_channel->audio_out);
		}
	}
	return TRUE;
}

static apt_bool_t mrcp_funasr_msg_signal(mrcp_funasr_msg_type_e type, mrcp_engine_channel_t *channel, mrcp_message_t *request)
{
	apt_bool_t status = FALSE;
	mrcp_funasr_channel_t *funasr_channel = channel->method_obj;
	mrcp_funasr_engine_t *funasr_engine = funasr_channel->funasr_engine;
	apt_task_t *task = apt_consumer_task_base_get(funasr_engine->task);
	apt_task_msg_t *msg = apt_task_msg_get(task);
	if(msg) {
		mrcp_funasr_msg_t *funasr_msg;
		msg->type = TASK_MSG_USER;
		funasr_msg = (mrcp_funasr_msg_t*) msg->data;

		funasr_msg->type = type;
		funasr_msg->channel = channel;
		funasr_msg->request = request;
		status = apt_task_msg_signal(task,msg);
	}
	return status;
}

static apt_bool_t mrcp_funasr_msg_process(apt_task_t *task, apt_task_msg_t *msg)
{
	mrcp_funasr_msg_t *funasr_msg = (mrcp_funasr_msg_t*)msg->data;
	switch(funasr_msg->type) {
		case FUNASR_MSG_OPEN_CHANNEL:
		{
			/* open channel: 连接 Gateway 内部 WS 通道并启动接收线程 */
			mrcp_funasr_channel_t *funasr_channel = funasr_msg->channel->method_obj;
			char err[256] = "";
			funasr_channel->gateway_ws = ws_client_connect(
				funasr_channel->gateway_host,funasr_channel->gateway_port,
				funasr_channel->gateway_path,err,sizeof(err));
			if(!funasr_channel->gateway_ws) {
				apt_log(FUNASR_LOG_MARK,APT_PRIO_WARNING,
					"Failed to Connect Gateway WS %s:%d: %s",
					funasr_channel->gateway_host,funasr_channel->gateway_port,err);
			} else {
				apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Connected to Gateway WS %s:%d",
					funasr_channel->gateway_host,funasr_channel->gateway_port);
				funasr_channel->ws_stop = 0;
				apr_thread_create(&funasr_channel->ws_thread,NULL,
					mrcp_funasr_ws_thread_main,funasr_channel,funasr_msg->channel->pool);
			}
			/* open channel and send asynch response */
			mrcp_engine_channel_open_respond(funasr_msg->channel,TRUE);
			break;
		}
		case FUNASR_MSG_CLOSE_CHANNEL:
		{
			/* close channel: 停止接收线程、通知 Gateway 结束、释放 WS 连接 */
			mrcp_funasr_channel_t *funasr_channel = funasr_msg->channel->method_obj;
			if(funasr_channel->ws_thread) {
				funasr_channel->ws_stop = 1;
				if(funasr_channel->gateway_ws) {
					ws_client_send_text(funasr_channel->gateway_ws,"{\"type\":\"end\"}",-1);
					ws_client_shutdown(funasr_channel->gateway_ws);
				}
				apr_status_t st;
				apr_thread_join(&st,funasr_channel->ws_thread);
				funasr_channel->ws_thread = NULL;
			}
			if(funasr_channel->gateway_ws) {
				ws_client_destroy(funasr_channel->gateway_ws);
				funasr_channel->gateway_ws = NULL;
			}
			if(funasr_channel->audio_out) {
				fclose(funasr_channel->audio_out);
				funasr_channel->audio_out = NULL;
			}

			mrcp_engine_channel_close_respond(funasr_msg->channel);
			break;
		}
		case FUNASR_MSG_REQUEST_PROCESS:
			mrcp_funasr_channel_request_dispatch(funasr_msg->channel,funasr_msg->request);
			break;
		default:
			break;
	}
	return TRUE;
}

/** 极简 JSON 字符串字段提取: 提取 {"key": "value"} 中的 value */
static apt_bool_t json_extract_string(const char *json, const char *key, char *out, int out_len)
{
	char pattern[128];
	const char *p;
	const char *colon;
	const char *q1;
	const char *q2;
	snprintf(pattern,sizeof(pattern),"\"%s\"",key);
	p = strstr(json,pattern);
	if(!p) {
		return FALSE;
	}
	colon = strchr(p + strlen(pattern),':');
	if(!colon) {
		return FALSE;
	}
	q1 = strchr(colon,'\"');
	if(!q1) {
		return FALSE;
	}
	q2 = strchr(q1 + 1,'\"');
	if(!q2) {
		return FALSE;
	}
	int len = (int)(q2 - q1 - 1);
	if(len >= out_len) {
		len = out_len - 1;
	}
	memcpy(out,q1 + 1,(size_t)len);
	out[len] = '\0';
	return TRUE;
}

/** Gateway WS 接收线程: 下行事件 -> MRCP 事件映射（Task 5） */
static void* APR_THREAD_FUNC mrcp_funasr_ws_thread_main(apr_thread_t *thread, void *obj)
{
	mrcp_funasr_channel_t *funasr_channel = (mrcp_funasr_channel_t*)obj;
	char buf[FUNASR_WS_RECV_BUF_SIZE];
	char event[64];
	char text[2048];

	while(!funasr_channel->ws_stop) {
		int n = ws_client_recv_text(funasr_channel->gateway_ws,buf,sizeof(buf),500);
		if(n < 0) {
			if(!funasr_channel->ws_stop) {
				apt_log(FUNASR_LOG_MARK,APT_PRIO_WARNING,"Gateway WS Connection Closed");
			}
			break;
		}
		if(n <= 0) {
			continue;
		}
		if(json_extract_string(buf,"event",event,sizeof(event)) != TRUE) {
			apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Gateway Message [%.200s]",buf);
			continue;
		}
		apt_log(FUNASR_LOG_MARK,APT_PRIO_INFO,"Gateway Event [%s]",event);

		/* ── Task 5: 事件映射 ── */
		if(strcmp(event,"started") == 0) {
			/* 识别已开始（无需映射 MRCP 事件） */
		}
		else if(strcmp(event,"speech_start") == 0) {
			/* 检测到语音 -> START-OF-INPUT */
			mrcp_funasr_start_of_input(funasr_channel);
		}
		else if(strcmp(event,"recognition_result") == 0) {
			/* 流式中间/最终结果: 保存最新 data.text */
			if(json_extract_string(buf,"text",text,sizeof(text)) == TRUE && text[0]) {
				if(funasr_channel->mutex) {
					apr_thread_mutex_lock(funasr_channel->mutex);
				}
				snprintf(funasr_channel->result_text,sizeof(funasr_channel->result_text),"%s",text);
				funasr_channel->result_len = (int)strlen(funasr_channel->result_text);
				if(funasr_channel->mutex) {
					apr_thread_mutex_unlock(funasr_channel->mutex);
				}
			}
		}
		else if(strcmp(event,"speech_end") == 0) {
			/* 最终结果 -> RECOGNITION-COMPLETE */
			if(json_extract_string(buf,"text",text,sizeof(text)) == TRUE && text[0]) {
				if(funasr_channel->mutex) {
					apr_thread_mutex_lock(funasr_channel->mutex);
				}
				snprintf(funasr_channel->result_text,sizeof(funasr_channel->result_text),"%s",text);
				funasr_channel->result_len = (int)strlen(funasr_channel->result_text);
				if(funasr_channel->mutex) {
					apr_thread_mutex_unlock(funasr_channel->mutex);
				}
				mrcp_funasr_recognition_complete(funasr_channel,RECOGNIZER_COMPLETION_CAUSE_SUCCESS);
			}
			else {
				/* 空文本: 用已保存的中间结果；仍为空 -> no-input-timeout */
				int have_text;
				if(funasr_channel->mutex) {
					apr_thread_mutex_lock(funasr_channel->mutex);
				}
				have_text = funasr_channel->result_len > 0;
				if(funasr_channel->mutex) {
					apr_thread_mutex_unlock(funasr_channel->mutex);
				}
				mrcp_funasr_recognition_complete(funasr_channel,
					have_text ? RECOGNIZER_COMPLETION_CAUSE_SUCCESS
					           : RECOGNIZER_COMPLETION_CAUSE_NO_INPUT_TIMEOUT);
			}
		}
		else if(strcmp(event,"finished") == 0) {
			/* 会话结束兜底: 尚未发完成事件则补发 */
			int have_text;
			apt_bool_t sent;
			apt_bool_t has_req;
			if(funasr_channel->mutex) {
				apr_thread_mutex_lock(funasr_channel->mutex);
			}
			have_text = funasr_channel->result_len > 0;
			sent = funasr_channel->recognition_sent;
			has_req = funasr_channel->recog_request != NULL;
			if(funasr_channel->mutex) {
				apr_thread_mutex_unlock(funasr_channel->mutex);
			}
			if(sent == FALSE && has_req == TRUE) {
				mrcp_funasr_recognition_complete(funasr_channel,
					have_text ? RECOGNIZER_COMPLETION_CAUSE_SUCCESS
					           : RECOGNIZER_COMPLETION_CAUSE_NO_INPUT_TIMEOUT);
			}
		}
		else if(strcmp(event,"error") == 0) {
			/* Gateway 错误 -> ERROR cause */
			mrcp_funasr_recognition_complete(funasr_channel,RECOGNIZER_COMPLETION_CAUSE_ERROR);
		}
	}
	return NULL;
}
