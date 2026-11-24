package com.floodgis.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 后端自检页：GET /status
 *
 * <h3>为什么路径从 "/" 挪到了 "/status"？</h3>
 * <p>这个类最早挂在 {@code @GetMapping("/")} 上，作用是让访问根路径时不至于看到
 * Spring Boot 的 "Whitelabel Error Page"（白底黑字的 404 页），免得误以为服务挂了。</p>
 *
 * <p>但演示包形态变了：前端（Vue + Cesium）已经打包进 jar 的
 * {@code BOOT-INF/classes/static/}，由内嵌 Tomcat 直接从根路径提供。
 * 此时如果这个 controller 还占着 "/"，Spring MVC 的 handler mapping 优先级高于
 * 静态资源处理器，浏览器打开 {@code http://localhost:8080/} 看到的就会是下面这张
 * 导航页，而不是应用本身——演示时第一屏就出事。</p>
 *
 * <p>所以根路径让给前端，这个页面退到 "/status"。它仍然有用：排查问题时先打开
 * 它，能看到服务活着、数据库通着，就能立刻把"后端问题"和"前端问题"区分开。</p>
 *
 * <p><b>注意返回值。</b>这里返回的是 <code>String</code>，但内容是 HTML。
 * {@code @RestController} 默认按文本返回，为了让它渲染成网页而不是显示源码，
 * 下面用 <code>produces</code> 指定了 <code>text/html</code>。</p>
 */
@RestController
public class HomeController {

    /**
     * produces = "text/html;charset=UTF-8" 告诉 Spring：
     * 这段字符串是网页，请按 HTML 返回，并按 UTF-8 编码（否则中文会变乱码）。
     */
    @GetMapping(value = "/status", produces = "text/html;charset=UTF-8")
    public String status() {
        // Java 15+ 的文本块，写 HTML 比一堆字符串拼接清爽得多
        return """
                <!doctype html>
                <html lang="zh-CN">
                <head>
                  <meta charset="UTF-8">
                  <title>城市内涝平台 · 后端服务</title>
                  <style>
                    body { font-family: -apple-system, "Microsoft YaHei", sans-serif;
                           background: #0d1117; color: #e6edf3; padding: 48px; line-height: 1.9; }
                    h1 { font-size: 20px; font-weight: 600; }
                    .ok { color: #3fb950; }
                    a { color: #3fa9f5; text-decoration: none; }
                    a:hover { text-decoration: underline; }
                    code { background: #161b22; padding: 2px 6px; border-radius: 4px; font-size: 13px; }
                    ul { margin-top: 8px; }
                    .tip { color: #8b98a8; font-size: 13px; margin-top: 24px; }
                  </style>
                </head>
                <body>
                  <h1>城市内涝风险可视化平台 · 后端服务 <span class="ok">运行中</span></h1>
                  <p>可用接口：</p>
                  <ul>
                    <li><a href="/status">GET /status</a>　
                        本页，后端与数据库的自检入口</li>
                    <li><a href="/api/health">GET /api/health</a>　
                        服务健康检查（含数据库、PostGIS 状态）</li>
                    <li><a href="/api/zones">GET /api/zones</a>　
                        易涝区 GeoJSON（可按 <code>?minLevel=3</code> 过滤）</li>
                  </ul>
                  <p class="tip">
                    <!--
                      这里的地址必须写 127.0.0.1，不能写 localhost，也不是笔误。
                      天地图 key 的域名白名单里只登记了 127.0.0.1：用 localhost 打开时
                      浏览器发出的 Referer 是 http://localhost:8080/，天地图判定域名不符
                      返回 403（code 301007），卫星底图的所有瓦片都会失败，
                      页面只剩一个蓝色地球，而其他功能看起来全都正常，极难自查。
                      127.0.0.1 与 localhost 在浏览器眼里是两个不同域名，白名单不认后者。
                      所以 href 写成绝对地址：从 localhost 进来的访问者点一下就能换到合法域名。
                    -->
                    前端已打包进本服务，直接访问
                    <a href="http://127.0.0.1:8080/">http://127.0.0.1:8080/</a>
                    即可看到地图页面，不需要另外启动 npm run dev。
                    注意用 127.0.0.1 而不是 localhost，否则卫星底图会全部 403。
                  </p>
                </body>
                </html>
                """;
    }
}
