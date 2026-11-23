package com.floodgis.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * 跨域（CORS）配置
 *
 * <h3>为什么需要这个类？</h3>
 * <p>浏览器有一条"同源策略"：协议 + 域名 + 端口 三者完全一致才算同一个源。</p>
 * <ul>
 *   <li>前端跑在 <code>http://localhost:5173</code>（Vite 开发服务器）</li>
 *   <li>后端跑在 <code>http://localhost:8080</code></li>
 * </ul>
 * <p>端口不同 = 跨域。浏览器会在真正发请求之前先发一个 OPTIONS 预检请求，
 * 如果后端没声明"我允许 5173 来访问"，请求就被浏览器拦掉，控制台报
 * <code>has been blocked by CORS policy</code>。</p>
 *
 * <h3>注意：本项目其实用了更省事的办法</h3>
 * <p>前端 <code>vite.config.js</code> 里配了代理（proxy），开发时浏览器请求的是
 * <code>localhost:5173/api/...</code>，由 Vite 服务器在中间转发给 8080。
 * 这样浏览器看来始终是"同源"，压根不会触发跨域。</p>
 * <p><b>这份配置是保险：</b>当你绕过 Vite、直接用浏览器或其他域名访问后端时，
 * 它能让请求正常通过。两种方案同时存在不会冲突。</p>
 */
@Configuration // 告诉 Spring：这是一个配置类，启动时请加载我
public class WebCorsConfig implements WebMvcConfigurer {

    /**
     * 重写这个方法就能往 Spring MVC 里塞自定义配置。
     * implements 之后不需要加 @Override 之外的任何注解，Spring 会自动调用。
     */
    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/api/**")                       // 只对 /api 开头的路径生效
                .allowedOrigins(                             // 允许哪些"源"来访问
                        "http://localhost:5173",
                        "http://127.0.0.1:5173"
                )
                .allowedMethods("GET", "POST", "PUT", "DELETE", "OPTIONS") // 允许的请求方法
                .allowedHeaders("*")                         // 允许携带任意请求头
                .allowCredentials(true)                      // 允许带 Cookie（以后做登录要用）
                .maxAge(3600);                               // 预检结果缓存 1 小时，少发点 OPTIONS
    }
}
