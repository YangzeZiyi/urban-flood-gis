package com.floodgis;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 城市内涝风险可视化平台 · 后端启动类
 *
 * <p>整个后端程序唯一有 main 方法的类。运行它 = 启动服务。</p>
 *
 * <p>{@code @SpringBootApplication} 是一个"三合一"的复合注解，拆开看是：</p>
 * <ul>
 *   <li>{@code @SpringBootConfiguration}  —— 声明这是配置类</li>
 *   <li>{@code @EnableAutoConfiguration}  —— 自动配置：看到 classpath 里有 web 相关的 jar，
 *       就自动把 Tomcat、Spring MVC、Jackson 全部装配好，不用我们写一行 XML</li>
 *   <li>{@code @ComponentScan}            —— 扫描"本包及子包"下所有带
 *       {@code @Controller / @Service / @Repository / @Configuration} 的类，注册进 Spring 容器</li>
 * </ul>
 *
 * <p><b>包路径很关键：</b>本类在 {@code com.floodgis} 包下，所以只有
 * {@code com.floodgis.*} 里的组件会被扫描到。如果把 Controller 放到别的顶级包，
 * 会出现"接口 404 但类明明写了"的经典问题。</p>
 */
@SpringBootApplication
public class FloodGisApplication {

    public static void main(String[] args) {
        // SpringApplication.run 做了三件事：
        //   1. 创建 Spring 容器（IoC 容器，用来存放所有 Bean）
        //   2. 启动内嵌 Tomcat，默认监听 8080
        //   3. 启动完成后阻塞住主线程，服务开始对外提供接口
        SpringApplication.run(FloodGisApplication.class, args);
    }
}
