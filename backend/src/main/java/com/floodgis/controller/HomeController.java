package com.floodgis.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 首页入口：GET /
 *
 * <h3>为什么要写这个类？</h3>
 * <p>不加它，直接访问 <code>http://localhost:8080/</code> 会看到 Spring Boot 的
 * "Whitelabel Error Page"（一张白底黑字的 404 报错页），很容易误以为服务挂了。
 * 实际上服务是好的，只是根路径没有登记任何接口——这在新手阶段非常误导人。</p>
 *
 * <p>这个类只做一件事：访问根路径时返回一个极简导航页，把已有接口列出来���
 * 点一下就能跳过去，不用再手敲 URL。</p>
 *
 * <p><b>注意返回值。</b>这里返回的是 <code>String</code>，但内容是 HTML。
 * {@code @RestController} 默认会按文本返回，浏览器收到后按 Content-Type 解析。
 * 为了让它渲染成网页而不是显示源码，下面用 <code>produces</code> 指定了
 * <code>text/html</code>。</p>
 */
@RestController
public class HomeController {

    /**
     * produces = "text/html;charset=UTF-8" 告诉 Spring：
     * 这段字符串是网页，请按 HTML 返回，并按 UTF-8 编码（否则中文会变乱码）。
     */
    @GetMapping(value = "/", produces = "text/html;charset=UTF-8")
    public String index() {
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
                    <li><a href="/api/health">GET /api/health</a>　
                        服务健康检查（含数据库、PostGIS 状态）</li>
                    <li><a href="/api/zones">GET /api/zones</a>　
                        易涝区 GeoJSON（可按 <code>?minLevel=3</code> 过滤）</li>
                  </ul>
                  <p class="tip">
                    前端页面在另一个端口：<a href="http://127.0.0.1:5173">http://127.0.0.1:5173</a>
                    （需先 npm run dev）
                  </p>
                </body>
                </html>
                """;
    }
}
