// static/script.js

document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('results-container');
    const statusDiv = document.getElementById('status');
    const loader = document.getElementById('loader');

    // --- 状态管理变量 ---
    let allData = []; // 存储从API获取的所有数据
    let currentIndex = 0; // 追踪当前已渲染到哪条数据
    const itemsPerPage = 12; // 每次加载的数量
    let isLoading = false; // 防止滚动时重复加载的标志

    // --- 渲染函数 ---
    // 负责将一小批数据显示在页面上
    const renderItems = () => {
        // 如果所有数据都已加载，则隐藏加载动画并停止
        if (currentIndex >= allData.length) {
            loader.classList.add('hidden');
            return;
        }

        // 获取下一批要渲染的数据
        const itemsToRender = allData.slice(currentIndex, currentIndex + itemsPerPage);
        
        itemsToRender.forEach(item => {
            const result = item.result;
            const card = document.createElement('div');
            card.className = 'result-card';
            const formattedDate = new Date(item.timestamp * 1000).toLocaleString('zh-CN', { hour12: false });
            const seaBlue = result['海蓝程度'] || 'N/A';
            const cloudCover = result['云层覆盖率'] || 'N/A';

            // 使用模板字符串构建卡片内容
            card.innerHTML = `
                <div class="card-header"><h2>${formattedDate}</h2></div>
                <div class="card-body">
                    <p>状态: <span class="value">${result.status}</span></p>
                    <p>海蓝程度: <span class="value">${seaBlue}</span></p>
                    <p>云层覆盖率: <span class="value">${cloudCover}</span></p>
                </div>
                <div class="image-gallery">
                    <figure>
                        <img src="/${item.output_directory}/03_ocean_only.png" alt="裁剪后图像" loading="lazy">
                        <figcaption>海洋区域</figcaption>
                    </figure>
                    <figure>
                        <img src="/${item.output_directory}/04_cloud_mask.png" alt="云层蒙版" loading="lazy">
                        <figcaption>云层蒙版</figcaption>
                    </figure>
                </div>
            `;
            container.appendChild(card);
        });

        // 更新索引
        currentIndex += itemsPerPage;
    };

    // --- 滚动事件处理函数 ---
    const handleScroll = () => {
        // 如果正在加载或所有数据都已加载完毕，则不执行任何操作
        if (isLoading || currentIndex >= allData.length) {
            return;
        }

        // 判断是否滚动到页面底部（留出300像素的缓冲）
        if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 300) {
            isLoading = true;
            loader.classList.remove('hidden'); // 显示加载动画

            // 模拟网络延迟，让加载动画可见，提升用户体验
            setTimeout(() => {
                renderItems();
                isLoading = false; // 加载完成，重置标志
            }, 500);
        }
    };

    // --- 初始化函数 ---
    const init = async () => {
        try {
            const response = await fetch('/results');
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            
            const data = await response.json();
            
            if (data.length === 0) {
                statusDiv.textContent = '暂无分析数据，请等待后台任务执行...';
                return;
            }

            // 将数据倒序（最新的在最前）并存储到全局变量
            allData = data.reverse();
            statusDiv.textContent = `共加载 ${allData.length} 条记录。向下滚动以查看更多。`;
            
            // 渲染首屏数据
            renderItems();

            // 监听滚动事件
            window.addEventListener('scroll', handleScroll);

        } catch (error) {
            console.error("获取数据失败:", error);
            statusDiv.textContent = '获取数据失败，请检查后端服务是否正常。';
        }
    };

    // 启动应用
    init();
});