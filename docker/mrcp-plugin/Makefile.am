AM_CPPFLAGS                = $(UNIMRCP_PLUGIN_INCLUDES)

plugin_LTLIBRARIES         = mrcpfunasr.la

mrcpfunasr_la_SOURCES       = src/mrcp_funasr_engine.c src/ws_client.c
mrcpfunasr_la_LDFLAGS       = $(UNIMRCP_PLUGIN_OPTS) -lpthread

include $(top_srcdir)/build/rules/uniplugin.am
